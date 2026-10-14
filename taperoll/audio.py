"""Audio input: level meters, the in-memory mixdown and streamed stem files."""

from __future__ import annotations

import logging
import queue
import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import sounddevice as sd

from .config import AudioConfig, ChannelConfig, Stem
from .naming import stem_filename

log = logging.getLogger(__name__)

PEAK_HOLD_SECONDS = 10.0
BLOCK_SECONDS = 0.05


def resolve_device(device: int | str | None) -> int | str | None:
    """Device indices may be configured as strings ("3"); names stay names."""
    if isinstance(device, str) and device.isdigit():
        return int(device)
    return device


def input_devices() -> list[tuple[int, str, int]]:
    """(index, name, input channel count) of every input-capable device."""
    return [
        (i, d["name"], int(d["max_input_channels"]))
        for i, d in enumerate(sd.query_devices())
        if d["max_input_channels"] > 0
    ]


def full_scale(dtype: np.dtype[Any] | str) -> float:
    return float(np.iinfo(str(np.dtype(dtype))).max)


def block_peak(samples: np.ndarray, scale: float) -> float:
    """Peak of an integer sample block as 0‥1 (int64 avoids abs() overflow at min)."""
    return (
        float(np.max(np.abs(samples.astype(np.int64)))) / scale if samples.size else 0.0
    )


@dataclass(slots=True)
class LevelMeter:
    """Fast-attack / slow-decay level plus a peak-hold marker, both 0‥1 linear."""

    level: float = 0.0
    hold: float = 0.0
    hold_since: float = 0.0  # time.monotonic() of the last hold update

    def update(self, peak: float, now: float) -> None:
        self.level = peak if peak > self.level else self.level * 0.85 + peak * 0.15
        if peak >= self.hold or now - self.hold_since > PEAK_HOLD_SECONDS:
            self.hold = peak
            self.hold_since = now


def int24_bytes(samples: np.ndarray) -> bytes:
    """Pack a 1-D int16/int32 array as little-endian 24-bit PCM."""
    s32 = samples.astype(np.int32)
    if samples.dtype == np.int16:
        s32 <<= 16
    # Keep the top three bytes of each little-endian int32 sample.
    return np.ascontiguousarray(s32).view(np.uint8).reshape(-1, 4)[:, 1:].tobytes()


class StemWriter:
    """Streams each stem channel to its own mono 24-bit WAV from a background thread.

    The audio callback only enqueues blocks; all file I/O happens here, so long
    multitrack takes never have to fit in memory and survive a crash on disk.
    """

    def __init__(
        self, folder: Path, stems: tuple[Stem, ...], take: str, sample_rate: int
    ) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        self.folder = folder
        self.paths = [folder / stem_filename(take, s.channel, s.name) for s in stems]
        self.peaks = [0.0] * len(stems)  # 0‥1 linear, for dropping silent stems
        self._files: list[wave.Wave_write] = []
        for path in self.paths:
            wf = wave.open(str(path), "wb")  # noqa: SIM115 — open for the whole take
            wf.setnchannels(1)
            wf.setsampwidth(3)
            wf.setframerate(sample_rate)
            self._files.append(wf)
        self._queue: queue.SimpleQueue[np.ndarray | None] = queue.SimpleQueue()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def put(self, block: np.ndarray) -> None:
        """Queue a (frames, n_stems) block. Called from the audio callback."""
        self._queue.put(block)

    def close(self) -> None:
        """Write everything still queued, then finalise the WAV headers."""
        self._queue.put(None)
        self._thread.join()
        for wf in self._files:
            wf.close()

    def _run(self) -> None:
        failed = False
        while (block := self._queue.get()) is not None:
            if failed:
                continue  # keep draining so the audio callback never blocks
            try:
                scale = full_scale(block.dtype)
                for i, wf in enumerate(self._files):
                    column = block[:, i]
                    self.peaks[i] = max(self.peaks[i], block_peak(column, scale))
                    wf.writeframesraw(int24_bytes(column))
            except (OSError, ValueError):
                log.exception("Stem write failed in %s", self.folder)
                failed = True


class AudioInput:
    """Streams audio continuously; keeps the mixdown while recording.

    The stream opens enough inputs to cover the mixdown and all stem channels.
    The mixdown is metered and buffered in memory; stem channels are handed to
    a StemWriter while a stems take is recording.
    """

    def __init__(self, audio: AudioConfig, channels: ChannelConfig) -> None:
        self.config = audio
        self.device = resolve_device(audio.device)
        self.device_name = ""
        self.stems = channels.stems
        self.stems_unavailable = "" if channels.stems else "no stems configured"
        self._open_channels = channels.max_channel
        self._mix_cols = [ch - 1 for ch in channels.mixdown]
        self._stem_cols = [s.channel - 1 for s in channels.stems]
        self.meters = [LevelMeter() for _ in self._mix_cols]
        self._scale = full_scale(audio.dtype)
        self._frames: list[np.ndarray] = []
        self._recording = False
        self._stem_writer: StemWriter | None = None
        self._lock = threading.Lock()
        self._stream: Any = None

    def start(self) -> None:
        device = self.device
        if device is not None:
            try:
                sd.query_devices(device)
            except Exception:  # ValueError for names, PortAudioError for bad indices
                log.warning(
                    "Device %r not found — falling back to system default.", device
                )
                device = self.device = None
        info = sd.query_devices(sd.default.device[0] if device is None else device)
        self.device_name = info["name"]
        available = int(info["max_input_channels"])
        if self._open_channels > available:
            # E.g. fell back to the laptop mic: record what's there, no stems.
            self._open_channels = available
            self._mix_cols = [c for c in self._mix_cols if c < available] or [0]
            self.meters = [LevelMeter() for _ in self._mix_cols]
            if self.stems:
                self.stems_unavailable = f"device has only {available} input(s)"
        self._stream = sd.InputStream(
            samplerate=self.config.sample_rate,
            channels=self._open_channels,
            dtype=self.config.dtype,
            device=device,
            blocksize=int(self.config.sample_rate * BLOCK_SECONDS),
            callback=self._callback,
        )
        self._stream.start()

    def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    def start_recording(self, stem_writer: StemWriter | None = None) -> None:
        with self._lock:
            self._frames = []
            self._stem_writer = stem_writer
            self._recording = True

    def stop_recording(self) -> list[np.ndarray]:
        with self._lock:
            self._recording = False
            self._stem_writer = None
            return self._frames

    def _callback(
        self, indata: np.ndarray, frames: int, time_info: Any, status: Any
    ) -> None:
        if status:
            log.warning("Audio input status: %s", status)
        now = time.monotonic()
        mix = indata[:, self._mix_cols]  # fancy indexing → already a copy
        for meter, column in zip(self.meters, mix.T, strict=True):
            meter.update(block_peak(column, self._scale), now)
        with self._lock:
            if self._recording:
                self._frames.append(mix)
                if self._stem_writer is not None:
                    self._stem_writer.put(indata[:, self._stem_cols])

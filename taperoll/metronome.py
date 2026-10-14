"""Sample-accurate click track, played to the output channels of the input device."""

from __future__ import annotations

import logging
from typing import Any

import numpy as np
import sounddevice as sd

from .config import MetronomeConfig

log = logging.getLogger(__name__)


class MetronomeError(Exception):
    pass


class Metronome:
    def __init__(
        self, config: MetronomeConfig | None = None, sample_rate: int = 48000
    ) -> None:
        self.config = config or MetronomeConfig()
        self.sample_rate = sample_rate
        self.bpm: int = self.config.bpm_default
        self.beats_per_bar: int = 4
        self.subdivision: bool = False  # play a quiet eighth-note click between beats
        self.out_device_name: str = ""
        self._stream: Any = None
        self._countdown: int = 0  # samples remaining until next beat
        self._beat_idx: int = 0  # which beat within the bar we're on
        # Subdivision clicks run on their own independent schedule, offset half a beat
        # from the main one, and keep ticking (silently) even while subdivision is off.
        # That way toggling the flag never resets or re-phases the beat grid.
        self._sub_countdown: int = self._period() // 2
        self._pending_click: np.ndarray | None = None  # click tail from the last buffer
        cfg = self.config
        self._click_normal = self._make_click(cfg.click_freq, cfg.volume)
        self._click_accent = self._make_click(cfg.click_freq_accent, cfg.volume)
        self._click_sub = self._make_click(
            cfg.click_freq, cfg.volume * cfg.subdivision_volume_mult
        )

    @property
    def active(self) -> bool:
        return self._stream is not None

    def set_bpm(self, bpm: int) -> None:
        self.bpm = max(self.config.bpm_min, min(self.config.bpm_max, bpm))

    def set_time_signature(self, beats_per_bar: int) -> None:
        self.beats_per_bar = beats_per_bar
        self._beat_idx = 0  # so the next beat is the accent

    def cycle_time_signature(self) -> None:
        sigs = self.config.time_sigs
        idx = sigs.index(self.beats_per_bar) if self.beats_per_bar in sigs else -1
        self.set_time_signature(sigs[(idx + 1) % len(sigs)])

    def start(self, device: int | str | None = None) -> None:
        """Start clicking on device (or the default output). Raises MetronomeError."""
        if self.active:
            return
        try:
            out_device, info = _output_device(device)
        except Exception as exc:  # PortAudio raises a variety of errors here
            log.exception("Metronome device query failed")
            raise MetronomeError(f"Metronome device error: {exc}") from exc
        log.info("Starting metronome on '%s', %d BPM", info["name"], self.bpm)
        # Reset beat position; countdown=0 fires the first click immediately.
        self._countdown = 0
        self._beat_idx = 0
        self._sub_countdown = self._period() // 2
        try:
            stream = sd.OutputStream(
                samplerate=self.sample_rate,
                channels=min(2, int(info["max_output_channels"])),
                dtype="float32",
                device=out_device,
                blocksize=int(self.sample_rate * 0.05),
                callback=self._callback,
            )
            stream.start()
        except Exception as exc:
            log.exception("Metronome stream failed")
            raise MetronomeError(f"Metronome stream error: {exc}") from exc
        self._stream = stream
        self.out_device_name = info["name"]

    def stop(self) -> None:
        if self._stream is None:
            return
        self._stream.stop()
        self._stream.close()
        self._stream = None
        self.out_device_name = ""

    def restart(self, device: int | str | None = None) -> None:
        """Restart a running metronome so the next beat is an accent on beat 1."""
        if self.active:
            self.stop()
            self.start(device)

    # ── click synthesis ───────────────────────────────────────────────────────

    def _period(self) -> int:
        return max(1, int(self.sample_rate * 60.0 / self.bpm))

    def _make_click(self, freq: int, volume: float) -> np.ndarray:
        dur = self.config.click_dur
        t = np.linspace(0, dur, int(self.sample_rate * dur), endpoint=False)
        return (np.sin(2 * np.pi * freq * t) * np.exp(-t * 300.0) * volume).astype(
            np.float32
        )

    # ── stream callback ───────────────────────────────────────────────────────

    def _callback(
        self, outdata: np.ndarray, frames: int, time_info: Any, status: Any
    ) -> None:
        if status:
            log.warning("Metronome audio underflow: %s", status)
        outdata.fill(0)
        # Apply any click tail carried over from the previous buffer boundary.
        if self._pending_click is not None:
            tail = self._pending_click
            n = min(len(tail), frames)
            outdata[:n] += tail[:n, None]
            self._pending_click = tail[n:] if n < len(tail) else None
        period = self._period()
        pos = 0
        countdown = self._countdown  # samples from `pos` to the next beat
        sub_countdown = self._sub_countdown  # samples from `pos` to the next sub-tick
        beat_idx = self._beat_idx
        while pos < frames:
            remaining = frames - pos
            delta = min(countdown, sub_countdown)
            if delta > remaining:
                countdown -= remaining
                sub_countdown -= remaining
                break
            c_start = pos + delta
            countdown -= delta
            sub_countdown -= delta
            if c_start >= frames:
                # Falls exactly on the buffer boundary: don't fire yet, the next
                # callback will fire it at position 0 (countdown is already 0 there).
                break
            # All click waveforms share the same length, so summing overlapping
            # clicks (rare: beat and sub-tick landing on the same sample) is safe.
            clicks = []
            fire_beat = countdown == 0
            if fire_beat:
                accent = beat_idx == 0 and self.beats_per_bar > 1
                clicks.append(self._click_accent if accent else self._click_normal)
                beat_idx = (beat_idx + 1) % self.beats_per_bar
                countdown = period
            fire_sub = sub_countdown == 0
            if fire_sub and self.subdivision:
                clicks.append(self._click_sub)
            if fire_beat:
                # Re-anchor the subdivision to exactly half of the *current* period,
                # measured from this beat. Without this, the sub-tick keeps stepping
                # by whatever period was in effect when it last fired, so a tempo
                # change leaves it permanently offset from the true midpoint —
                # audible as swing instead of even eighth notes.
                sub_countdown = period // 2
            elif fire_sub:
                sub_countdown = period
            if clicks:
                combined = np.sum(clicks, axis=0)
                c_end = min(c_start + len(combined), frames)
                outdata[c_start:c_end] += combined[: c_end - c_start, None]
                # If the click extends past the buffer, carry the tail forward.
                if c_end < c_start + len(combined):
                    self._pending_click = combined[c_end - c_start :]
            pos = c_start
        self._countdown = countdown
        self._sub_countdown = sub_countdown
        self._beat_idx = beat_idx


def _output_device(device: int | str | None) -> tuple[int | str, Any]:
    """The device to click on: the given one if it has outputs, else the default output."""
    if device is not None:
        info = sd.query_devices(device)
        if int(info["max_output_channels"]) > 0:
            return device, info
        log.info("'%s' has no outputs; using the default output device.", info["name"])
    default_out = sd.default.device[1]
    info = sd.query_devices(default_out)
    if int(info["max_output_channels"]) == 0:
        raise MetronomeError("No output device with output channels found")
    return default_out, info

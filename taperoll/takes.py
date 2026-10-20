"""What happens to a take after it stops: save, encode, rate."""

from __future__ import annotations

import logging
import shutil
import subprocess
import threading
import wave
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .activity import ActivityLog
from .audio import StemWriter
from .config import Config
from .naming import stems_dir_for, take_stem, with_rating

log = logging.getLogger(__name__)


class RatingSlot:
    """Hand-off of a take's rating from the UI to its processing thread.

    The thread blocks in wait() until the UI calls resolve() with a rating
    (1–5) or None (skipped), or discard() to delete the take. Only the first
    call counts.
    """

    def __init__(self) -> None:
        self.rating: int | None = None
        self.discarded = False
        self._done = threading.Event()

    def resolve(self, rating: int | None) -> None:
        if not self._done.is_set():
            self.rating = rating
            self._done.set()

    def discard(self) -> None:
        if not self._done.is_set():
            self.discarded = True
            self._done.set()

    def wait(self) -> int | None:
        self._done.wait()
        return self.rating


@dataclass(slots=True)
class Take:
    ts: str  # YYYY-MM-DD_HH-MM-SS when recording started
    song_name: str
    frames: list[np.ndarray] = field(default_factory=list)  # mixdown blocks
    stem_writer: StemWriter | None = None
    rating: RatingSlot = field(default_factory=RatingSlot)

    @property
    def name(self) -> str:
        return take_stem(self.ts, self.song_name)

    @property
    def date(self) -> str:
        return self.ts[:10]


def normalize(audio: np.ndarray, target_db: float) -> np.ndarray:
    """Scale integer audio up so its peak hits target_db (dBFS). Never scales down."""
    max_val = float(np.iinfo(audio.dtype).max)
    peak = float(np.max(np.abs(audio.astype(np.float64)), initial=0.0))
    gain = max_val * 10 ** (target_db / 20.0) / peak if peak else 1.0
    if gain <= 1.0:
        return audio
    return np.clip(audio.astype(np.float64) * gain, -max_val, max_val).astype(
        audio.dtype
    )


def write_wav(path: Path, audio: np.ndarray, sample_rate: int) -> None:
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(audio.shape[1])
        wf.setsampwidth(audio.dtype.itemsize)
        wf.setframerate(sample_rate)
        wf.writeframes(audio.tobytes())


def ffmpeg(*args: str) -> bool:
    """Run ffmpeg; True on success.

    It runs in its own session, so a Ctrl+C while quitting doesn't abort an
    encode halfway.
    """
    proc = subprocess.run(
        ["ffmpeg", "-y", *args],
        capture_output=True,
        start_new_session=True,
        check=False,
    )
    return proc.returncode == 0


def finalize_stems(writer: StemWriter, folder: Path, config: Config) -> tuple[int, int]:
    """Close the writer, drop silent stems, convert the rest into folder.

    Stems are converted to config.stems.format (FLAC or MP3).

    Returns (kept, dropped). If every stem was silent, no folder is created.
    Without ffmpeg the stems stay WAV.
    """
    writer.close()
    threshold = 10 ** (config.stems.silence_db / 20.0)
    fmt = config.stems.format
    codec = (
        ["-c:a", "flac"] if fmt == "flac" else ["-b:a", config.recording.mp3_bitrate]
    )
    has_ffmpeg = shutil.which("ffmpeg") is not None
    kept = dropped = 0
    for wav_path, peak in zip(writer.paths, writer.peaks, strict=True):
        if peak < threshold:
            wav_path.unlink()
            dropped += 1
            continue
        kept += 1
        if has_ffmpeg:
            if ffmpeg(
                "-i", str(wav_path), *codec, str(wav_path.with_suffix(f".{fmt}"))
            ):
                wav_path.unlink()
            else:
                log.error("%s encode failed for %s, keeping WAV", fmt, wav_path)
    if kept:
        writer.folder.rename(folder)
    else:
        shutil.rmtree(writer.folder)
    return kept, dropped


class TakeProcessor:
    """Runs a finished take through the pipeline, reporting to the activity log.

    1. save the (normalised) mixdown WAV, encode it to MP3
    2. finish the stems: drop silent ones, convert to FLAC/MP3
    3. wait for the rating, then rename mixdown and stems folder accordingly
       (or delete them all if the take was discarded)

    Uploading is separate: `--upload` sends what's in the recordings folder.
    """

    def __init__(self, config: Config, activity: ActivityLog) -> None:
        self.config = config
        self.activity = activity

    def process(self, take: Take) -> None:
        def status(step: str, text: str) -> None:
            self.activity.add(text, key=(take.name, step))

        status("save", "Saving…")
        mixdown = wav = self._save_mixdown(take)
        size_mb = mixdown.stat().st_size / 1024 / 1024
        duration = sum(len(f) for f in take.frames) / self.config.audio.sample_rate
        status("save", f"Saved  {mixdown.name}  {duration:.0f}s / {size_mb:.1f} MB")

        if self.config.recording.encode_mp3 and shutil.which("ffmpeg"):
            status("encode", "Encoding…")
            if mp3 := self._encode_mp3(mixdown):
                mixdown = mp3
                status("encode", f"Encoded  {mp3.name}")
            else:
                status("encode", "Encoding FAILED — keeping WAV")

        stems_dir: Path | None = None
        if take.stem_writer is not None:
            status("stems", "Saving stems…")
            stems_dir = stems_dir_for(mixdown)
            kept, dropped = finalize_stems(take.stem_writer, stems_dir, self.config)
            stems_dir = stems_dir if kept else None
            silent = f", {dropped} silent dropped" if dropped else ""
            status("stems", f"Stems  {kept} saved{silent}")

        # Hold the final names until the take is rated (or the rating is skipped).
        rating = take.rating.wait()
        if take.rating.discarded:
            wav.unlink(missing_ok=True)  # still there if MP3 encoding kept it
            mixdown.unlink(missing_ok=True)
            if stems_dir is not None:
                shutil.rmtree(stems_dir, ignore_errors=True)
            self.activity.add(f"✗ Deleted  {take.name}")
            return
        if rating is not None:
            mixdown = mixdown.rename(with_rating(mixdown, rating))
            if stems_dir is not None:
                stems_dir.rename(stems_dir_for(mixdown))
        self.activity.add("Ready ✓")

    def _save_mixdown(self, take: Take) -> Path:
        rec = self.config.recording
        self.config.output_dir.mkdir(parents=True, exist_ok=True)
        path = self.config.output_dir / f"{take.name}.wav"
        audio = np.concatenate(take.frames, axis=0)
        if rec.normalize:
            audio = normalize(audio, rec.normalize_target_db)
        write_wav(path, audio, self.config.audio.sample_rate)
        return path

    def _encode_mp3(self, wav: Path) -> Path | None:
        mp3 = wav.with_suffix(".mp3")
        if not ffmpeg(
            "-i", str(wav), "-b:a", self.config.recording.mp3_bitrate, str(mp3)
        ):
            return None
        if self.config.recording.delete_wav_after_encode:
            wav.unlink()
        return mp3

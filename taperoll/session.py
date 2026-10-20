"""The recorder's state and everything you can do with it, independent of any UI."""

from __future__ import annotations

import datetime
import logging
import shutil
import threading
import time
from collections.abc import Callable, Sequence
from typing import Protocol

from .activity import ActivityLog
from .audio import AudioInput, StemWriter
from .config import Config
from .metronome import Metronome, MetronomeError
from .naming import PARTIAL_SUFFIX, STEMS_SUFFIX, timestamp
from .setlist import Setlist, add_to_setlist, load_setlist, remove_from_setlist
from .songs import SongSettings, SongStore
from .takes import Take

log = logging.getLogger(__name__)


class TakeHandler(Protocol):
    """Runs a stopped take through the pipeline (see takes.TakeProcessor)."""

    def process(self, take: Take) -> None: ...


class Session:
    """One run of the recorder: the current song, the take being recorded, the
    take waiting for its rating, the setlist position and the activity log."""

    def __init__(
        self,
        config: Config,
        audio: AudioInput,
        metronome: Metronome,
        songs: SongStore,
        processor: TakeHandler,
        activity: ActivityLog,
        *,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime.datetime] = datetime.datetime.now,
    ) -> None:
        self.config = config
        self.audio = audio
        self.metronome = metronome
        self.songs = songs
        self.processor = processor
        self.activity = activity
        self._clock = clock
        self._now = now

        self.song_name = ""
        self.stems_mode = False  # record stems (remembered per song)
        self.take: Take | None = None  # being recorded
        self.pending_rating: Take | None = None  # stopped, waiting for its rating
        self.setlist = Setlist()
        self.setlist_idx = -1  # -1 = not positioned yet
        self._rec_started = 0.0
        self._threads: list[threading.Thread] = []

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start(self, warnings: Sequence[str] = ()) -> None:
        """Open the audio stream and load the setlist, logging any warnings."""
        self.audio.start()
        self.setlist = load_setlist(self.config.setlist_path, self.songs)
        for warning in [*warnings, *self.setlist.warnings]:
            self.activity.add(f"⚠ {warning}")
        if self.setlist:
            self.activity.add(
                f"Setlist loaded: {len(self.setlist)} songs from "
                f"{self.config.setlist.file}  (↑↓ to navigate)"
            )

    def shutdown(self) -> list[threading.Thread]:
        """Stop audio; returns the processing threads that are still running."""
        self.rate(None, quiet=True)  # a take waiting for its rating stays unrated
        self.audio.stop()
        self.metronome.stop()
        return [t for t in self._threads if t.is_alive()]

    @property
    def recording(self) -> bool:
        return self.take is not None

    @property
    def elapsed(self) -> float:
        return self._clock() - self._rec_started if self.recording else 0.0

    @property
    def can_quit(self) -> bool:
        return not self.recording

    # ── recording ─────────────────────────────────────────────────────────────

    def toggle_recording(self) -> None:
        if self.take is None:
            self._start_take()
        else:
            self._stop_take(self.take)

    def _start_take(self) -> None:
        self.rate(None)  # starting the next take skips rating the previous one
        take = Take(ts=timestamp(self._now()), song_name=self.song_name)
        if self.stems_mode and not self.audio.stems_unavailable:
            folder = (
                self.config.output_dir / f"{take.name}{STEMS_SUFFIX}{PARTIAL_SUFFIX}"
            )
            try:
                take.stem_writer = StemWriter(
                    folder, self.audio.stems, take.name, self.config.audio.sample_rate
                )
            except OSError as exc:
                log.exception("Could not start stems")
                self.activity.add(f"Stems failed to start: {exc}")
        self.audio.start_recording(take.stem_writer)
        self.take = take
        self._rec_started = self._clock()
        label = f" ♪ {self.song_name}" if self.song_name else ""
        stems = "  + stems" if take.stem_writer else ""
        self.activity.add(f"● Recording  {take.ts}{label}{stems}")

    def _stop_take(self, take: Take) -> None:
        take.frames = self.audio.stop_recording()
        duration = self.elapsed
        self.take = None
        if duration < self.config.recording.min_seconds:
            self.activity.add(f"■ Cancelled  {duration:.0f}s (too short, discarded)")
            if take.stem_writer is not None:
                take.stem_writer.close()
                shutil.rmtree(take.stem_writer.folder, ignore_errors=True)
            return
        self.activity.add(f"■ Stopped  {duration:.0f}s")
        self._save_song()
        self.pending_rating = take
        thread = threading.Thread(target=self._process, args=(take,), daemon=True)
        thread.start()
        self._threads.append(thread)

    def _process(self, take: Take) -> None:
        try:
            self.processor.process(take)
        except Exception as exc:
            log.exception("Processing %s failed", take.name)
            self.activity.add(f"⚠ Processing {take.name} failed: {exc}")

    def rate(self, rating: int | None, *, quiet: bool = False) -> None:
        """Rate the take waiting for its rating (None = skip), releasing its processing."""
        take, self.pending_rating = self.pending_rating, None
        if take is None:
            return
        take.rating.resolve(rating)
        if quiet:
            return
        if rating is None:
            self.activity.add(f"Rating skipped  {take.name}")
        else:
            self.activity.add(f"{'★' * rating}{'☆' * (5 - rating)}  {take.name}")

    def discard(self) -> None:
        """Delete the take waiting for its rating, mixdown and stems alike."""
        take, self.pending_rating = self.pending_rating, None
        if take is not None:
            take.rating.discard()

    # ── songs and setlist ─────────────────────────────────────────────────────

    def select_song(self, name: str) -> None:
        """Make name the current song. A known song brings back its saved settings."""
        if self.recording:
            return
        self.song_name = name.strip()
        if settings := self.songs.get(self.song_name):
            self._apply(settings)
        self._save_song()

    def setlist_step(self, delta: int) -> None:
        """Move through the setlist (and load that song) by delta positions."""
        if not self.setlist or self.recording:
            return
        idx = max(0, min(len(self.setlist) - 1, self.setlist_idx + delta))
        if idx == self.setlist_idx:
            return
        self.setlist_idx = idx
        self.song_name = self.setlist.songs[idx]
        if settings := self.songs.get(self.song_name):
            self._apply(settings)
        stems = "  stems" if self.stems_mode else ""
        self.activity.add(
            f"→ [{idx + 1}/{len(self.setlist)}] {self.song_name}  "
            f"{self.metronome.bpm} BPM  {self.metronome.beats_per_bar}/4{stems}"
        )
        try:
            self.metronome.restart(self.audio.device)
        except MetronomeError as exc:
            self.activity.add(str(exc))

    def setlist_add(self) -> None:
        """Append the current song to setlist.txt."""
        if self.recording:
            return
        if not self.song_name:
            self.activity.add("No song name set — press n first")
            return
        try:
            add_to_setlist(self.config.setlist_path, self.song_name)
        except OSError as exc:
            self.activity.add(f"Setlist save failed: {exc}")
            return
        self._reload_setlist()
        if self.song_name in self.setlist.songs:
            self.setlist_idx = self.setlist.songs.index(self.song_name)
        self.activity.add(f'+ Added "{self.song_name}" to setlist')

    def setlist_remove(self) -> None:
        """Remove the selected (or current) song from setlist.txt."""
        if self.recording:
            return
        if self.setlist_idx >= 0:
            target = self.setlist.songs[self.setlist_idx]
        elif self.song_name in self.setlist.songs:
            target = self.song_name
        else:
            self.activity.add(
                "Nothing to remove — navigate to or set a song in the setlist"
            )
            return
        try:
            remove_from_setlist(self.config.setlist_path, target)
        except OSError as exc:
            self.activity.add(f"Setlist save failed: {exc}")
            return
        self._reload_setlist()
        self.setlist_idx = min(self.setlist_idx, len(self.setlist) - 1)
        self.activity.add(f'− Removed "{target}" from setlist')

    def _reload_setlist(self) -> None:
        self.setlist = load_setlist(self.config.setlist_path, self.songs)

    def _apply(self, settings: SongSettings) -> None:
        self.metronome.set_bpm(settings.bpm)
        self.metronome.set_time_signature(settings.sig)
        self.stems_mode = settings.stems

    def _save_song(self) -> None:
        """Remember the current BPM, time signature and stems mode for this song."""
        if not self.song_name:
            return
        settings = SongSettings(
            self.metronome.bpm, self.metronome.beats_per_bar, self.stems_mode
        )
        try:
            self.songs.save(self.song_name, settings)
        except OSError as exc:
            log.exception("Could not save song store")
            self.activity.add(f"Song store save failed: {exc}")

    # ── metronome and stems ───────────────────────────────────────────────────

    def toggle_metronome(self) -> None:
        if self.metronome.active:
            self.metronome.stop()
            return
        try:
            self.metronome.start(self.audio.device)
        except MetronomeError as exc:
            self.activity.add(str(exc))

    def set_bpm(self, bpm: int) -> None:
        self.metronome.set_bpm(bpm)
        self._save_song()

    def cycle_time_signature(self) -> None:
        self.metronome.cycle_time_signature()
        self._save_song()

    def toggle_subdivision(self) -> None:
        # The sub-tick schedule keeps running in the background regardless of
        # this flag, so toggling it never resets or re-phases the beat grid.
        self.metronome.subdivision = not self.metronome.subdivision
        self.activity.add(
            f"Subdivision {'ON' if self.metronome.subdivision else 'OFF'}"
        )

    def toggle_stems(self) -> None:
        if self.recording:
            return
        if not self.audio.stems:
            self.activity.add("No stems configured — see config.example.toml")
            return
        self.stems_mode = not self.stems_mode
        self._save_song()
        msg = f"Stems {'ON' if self.stems_mode else 'OFF'}"
        if self.stems_mode and self.audio.stems_unavailable:
            msg += f" (unavailable: {self.audio.stems_unavailable})"
        self.activity.add(msg)

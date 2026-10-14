"""The song store, and audio capture: meters, mixdown routing, stem files."""

from __future__ import annotations

import json
import time
import wave
from pathlib import Path

import numpy as np
import pytest

from taperoll.activity import ActivityLog
from taperoll.audio import (
    AudioInput,
    LevelMeter,
    StemWriter,
    input_devices,
    int24_bytes,
    resolve_device,
)
from taperoll.config import AudioConfig, ChannelConfig, Stem
from taperoll.songs import SongSettings, SongStore

STEMS = (Stem(3, "Kick"), Stem(4, "Bass DI"))


# ─── songs ────────────────────────────────────────────────────────────────────


def test_missing_song_store_is_empty(tmp_path: Path) -> None:
    store, warning = SongStore.load(tmp_path / "songs.json")
    assert (len(store), warning) == (0, None)


def test_song_store_round_trip_and_old_format(tmp_path: Path) -> None:
    path = tmp_path / "songs.json"
    path.write_text(
        json.dumps({"Old": 100, "New": {"bpm": 90, "sig": 3, "stems": True}})
    )
    store, warning = SongStore.load(path)
    assert warning is None
    assert store.get("Old") == SongSettings(bpm=100)
    assert store.get("New") == SongSettings(90, 3, True)
    store.save("Third", SongSettings(120, 4))
    assert "Third" in store
    assert sorted(store) == ["New", "Old", "Third"]
    assert json.loads(path.read_text())["Old"] == {"bpm": 100, "sig": 4, "stems": False}


def test_corrupt_song_store_warns(tmp_path: Path) -> None:
    path = tmp_path / "songs.json"
    path.write_text("{not json")
    store, warning = SongStore.load(path)
    assert len(store) == 0
    assert warning is not None and "Could not load songs.json" in warning


def test_suggestions_rank_prefix_then_substring_then_fuzzy() -> None:
    store = SongStore(
        Path("songs.json"),
        {n: SongSettings() for n in ("Circles", "Dancing in Circles", "Circus")},
    )
    assert store.suggestions("circ") == ["Circles", "Circus", "Dancing in Circles"]
    assert store.suggestions("cirlces")[0] == "Circles"  # typo → fuzzy match
    assert store.suggestions("") == []


# ─── meters & devices ─────────────────────────────────────────────────────────


def test_level_meter_attack_decay_and_hold() -> None:
    meter = LevelMeter()
    meter.update(0.8, now=0.0)
    assert (meter.level, meter.hold) == (0.8, 0.8)
    meter.update(0.0, now=1.0)
    assert meter.level == pytest.approx(0.68)  # slow decay
    assert meter.hold == 0.8  # held
    meter.update(0.1, now=20.0)
    assert meter.hold == 0.1  # hold expired


def test_input_devices_and_device_indices() -> None:
    assert input_devices() == [(0, "Laptop Mic", 1), (1, "Fake DL32S", 8)]
    assert resolve_device("3") == 3
    assert resolve_device("DL32S") == "DL32S"
    assert resolve_device(None) is None


def test_int24_bytes() -> None:
    assert int24_bytes(np.array([0x12345678, -1], dtype=np.int32)) == bytes(
        [0x56, 0x34, 0x12, 0xFF, 0xFF, 0xFF]
    )
    assert int24_bytes(np.array([0x1234], dtype=np.int16)) == bytes([0x00, 0x34, 0x12])


# ─── AudioInput ───────────────────────────────────────────────────────────────


def _input(device: str = "Fake DL32S", stems: tuple[Stem, ...] = STEMS) -> AudioInput:
    return AudioInput(
        AudioConfig(device=device), ChannelConfig(mixdown=(1, 2), stems=stems)
    )


def _block(n_frames: int = 100, n_channels: int = 4) -> np.ndarray:
    # Channel c holds the constant value (c + 1) * 1000, so routing is visible.
    row = (np.arange(n_channels, dtype=np.int32) + 1) * 1000
    return np.tile(row, (n_frames, 1))


def test_mixdown_and_stems_are_routed_separately(tmp_path: Path) -> None:
    audio = _input()
    writer = StemWriter(tmp_path / "x", STEMS, "take", 48000)
    audio.start_recording(writer)
    audio._callback(_block(), 100, None, None)
    frames = audio.stop_recording()
    audio._callback(_block(), 100, None, None)  # after stop: ignored
    writer.close()

    assert len(frames) == 1
    assert (frames[0][:, 0] == 1000).all() and (frames[0][:, 1] == 2000).all()
    assert [p.name for p in writer.paths] == ["take_03_kick.wav", "take_04_bass-di.wav"]
    with wave.open(str(writer.paths[1])) as wf:
        assert (wf.getsampwidth(), wf.getnchannels(), wf.getnframes()) == (3, 1, 100)
    assert writer.peaks[1] == pytest.approx(4000 / np.iinfo(np.int32).max)


def test_meters_follow_the_mixdown_channels() -> None:
    audio = _input()
    audio._callback(_block(), 100, None, None)
    assert audio.meters[0].level < audio.meters[1].level


def test_stream_on_the_configured_device() -> None:
    audio = _input()
    audio.start()
    try:
        time.sleep(0.15)
        assert audio.device_name == "Fake DL32S"
        assert audio.stems_unavailable == ""
        assert audio.meters[0].level > 0
    finally:
        audio.stop()


def test_falls_back_to_default_device_without_stems() -> None:
    audio = _input(device="Not Plugged In")
    audio.start()
    audio.stop()
    assert audio.device is None
    assert audio.device_name == "Laptop Mic"
    assert len(audio.meters) == 1
    assert audio.stems_unavailable == "device has only 1 input(s)"


def test_without_stems_configured() -> None:
    assert _input(stems=()).stems_unavailable == "no stems configured"


# ─── activity log ─────────────────────────────────────────────────────────────


def test_activity_log_keys_replace_and_old_lines_scroll_away() -> None:
    log = ActivityLog(maxlen=3)
    log.add("Saving…", key="a")
    log.add("other")
    log.add("Saved", key="a")
    assert log.lines() == ["Saved", "other"]
    log.add("x")
    log.add("y")
    assert log.lines() == ["other", "x", "y"]
    log.add("Uploaded", key="a")  # its line scrolled away → appended
    assert log.lines() == ["x", "y", "Uploaded"]

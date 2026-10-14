"""Session actions and the TUI's rendering and key handling, without a terminal."""

from __future__ import annotations

import datetime
import json
import threading
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from taperoll.activity import ActivityLog
from taperoll.audio import AudioInput
from taperoll.config import AudioConfig, ChannelConfig, Config, Stem
from taperoll.metronome import Metronome, MetronomeError
from taperoll.session import Session
from taperoll.songs import SongSettings, SongStore
from taperoll.takes import Take
from taperoll.tui import BpmPrompt, NamePrompt, Style, Tui, meter_row

NOW = datetime.datetime(2026, 10, 8, 20, 14, 3)
STEMS = ChannelConfig(mixdown=(1, 2), stems=(Stem(3, "kick"),))


@dataclass
class FakeProcessor:
    takes: list[Take] = field(default_factory=list)
    fail: bool = False
    done: threading.Event = field(default_factory=threading.Event)

    def process(self, take: Take) -> None:
        self.takes.append(take)
        self.done.set()
        if self.fail:
            raise RuntimeError("disk full")


class Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


@dataclass
class Env:
    session: Session
    processor: FakeProcessor
    clock: Clock
    home: Path

    @property
    def log(self) -> list[str]:
        return self.session.activity.lines()

    def record(self, seconds: float = 20) -> None:
        self.session.toggle_recording()
        self.clock.t += seconds
        self.session.toggle_recording()


def _env(
    tmp_path: Path,
    *,
    channels: ChannelConfig | None = None,
    songs: dict[str, SongSettings] | None = None,
    setlist: str | None = None,
) -> Env:
    channels = channels or ChannelConfig()
    config = Config(home=tmp_path, channels=channels)
    if setlist is not None:
        config.setlist_path.write_text(setlist)
    processor = FakeProcessor()
    clock = Clock()
    session = Session(
        config,
        AudioInput(AudioConfig(device="Fake DL32S"), channels),
        Metronome(),
        SongStore(config.songs_path, dict(songs or {})),
        processor,
        ActivityLog(maxlen=20),
        clock=clock,
        now=lambda: NOW,
    )
    session.start(["something odd"])
    return Env(session, processor, clock, tmp_path)


@pytest.fixture
def env(tmp_path: Path):
    e = _env(tmp_path)
    yield e
    e.session.shutdown()


# ─── recording & rating ───────────────────────────────────────────────────────


def test_startup_warnings_are_logged(env: Env) -> None:
    assert env.log == ["⚠ something odd"]


def test_recording_a_take(env: Env) -> None:
    env.session.select_song("Circles")
    env.session.toggle_recording()
    assert env.session.recording and not env.session.can_quit
    env.clock.t += 20
    assert env.session.elapsed == 20
    env.session.toggle_recording()
    assert env.processor.done.wait(1)

    (take,) = env.processor.takes
    assert take.name == "circles_2026-10-08_20-14-03"
    assert take.frames is not None
    assert env.session.pending_rating is take
    assert env.log[-2:] == [
        "● Recording  2026-10-08_20-14-03 ♪ Circles",
        "■ Stopped  20s",
    ]
    assert (
        json.loads(env.session.config.songs_path.read_text())["Circles"]["bpm"] == 120
    )


def test_rating_and_skipping(env: Env) -> None:
    env.record()
    take = env.session.pending_rating
    assert take is not None
    env.session.rate(4)
    assert take.rating.rating == 4
    assert env.session.pending_rating is None
    assert env.log[-1] == "★★★★☆  2026-10-08_20-14-03"
    env.session.rate(5)  # nothing pending: no-op
    assert env.log[-1] == "★★★★☆  2026-10-08_20-14-03"


def test_starting_the_next_take_skips_the_rating(env: Env) -> None:
    env.record()
    take = env.session.pending_rating
    env.session.toggle_recording()
    assert take is not None and take.rating.wait() is None
    assert "Rating skipped  2026-10-08_20-14-03" in env.log


def test_too_short_take_is_discarded_with_its_stems(tmp_path: Path) -> None:
    env = _env(tmp_path, channels=STEMS)
    env.session.toggle_stems()
    env.record(seconds=2)
    env.session.shutdown()
    assert env.log[-1] == "■ Cancelled  2s (too short, discarded)"
    assert env.processor.takes == []
    assert not list(env.session.config.output_dir.glob("*_stems*"))


def test_stems_take_gets_a_stem_writer(tmp_path: Path) -> None:
    env = _env(tmp_path, channels=STEMS)
    env.session.toggle_stems()
    env.session.toggle_recording()
    assert env.log[-1].endswith("  + stems")
    env.clock.t += 20
    env.session.toggle_recording()
    env.processor.done.wait(1)
    env.session.shutdown()
    (take,) = env.processor.takes
    assert take.stem_writer is not None
    assert take.stem_writer.folder.name == f"{take.name}_stems.partial"


def test_shutdown_releases_the_pending_rating_quietly(env: Env) -> None:
    env.record()
    take = env.session.pending_rating
    lines = env.log
    env.session.shutdown()
    assert take is not None and take.rating.wait() is None
    assert env.log == lines


def test_processing_errors_are_reported(env: Env) -> None:
    env.processor.fail = True
    env.record()
    for thread in env.session.shutdown():
        thread.join()
    assert env.log[-1] == "⚠ Processing 2026-10-08_20-14-03 failed: disk full"


# ─── songs, setlist, metronome, stems ─────────────────────────────────────────


def test_selecting_a_known_song_restores_its_settings(tmp_path: Path) -> None:
    env = _env(tmp_path, channels=STEMS, songs={"Dare": SongSettings(95, 3, True)})
    env.session.select_song(" Dare ")
    assert (env.session.song_name, env.session.metronome.bpm) == ("Dare", 95)
    assert env.session.metronome.beats_per_bar == 3
    assert env.session.stems_mode
    env.session.select_song("Brand New")
    assert env.session.metronome.bpm == 95  # keeps the current settings
    assert env.session.songs.get("Brand New") == SongSettings(95, 3, True)


def test_setlist_navigation(tmp_path: Path) -> None:
    songs = {"A": SongSettings(100, 4), "B": SongSettings(140, 3)}
    env = _env(tmp_path, songs=songs, setlist="A\nB\nUnknown\n")
    s = env.session
    assert '⚠ Setlist line 3 "Unknown" → no match in songs.json' in env.log
    s.metronome.start()
    s.setlist_step(-1)  # from "not positioned": goes to the first song
    assert (s.setlist_idx, s.song_name) == (0, "A")
    s.setlist_step(+1)
    s.setlist_step(+1)  # stays on the last song
    assert (s.setlist_idx, s.metronome.bpm, s.metronome.beats_per_bar) == (1, 140, 3)
    assert env.log[-1] == "→ [2/2] B  140 BPM  3/4"
    assert s.metronome.active  # restarted on the new song
    s.metronome.stop()


def test_setlist_add_and_remove(tmp_path: Path) -> None:
    env = _env(
        tmp_path, songs={"A": SongSettings(), "B": SongSettings()}, setlist="A\n"
    )
    s = env.session
    s.setlist_add()
    assert env.log[-1] == "No song name set — press n first"
    s.setlist_remove()
    assert env.log[-1] == "Nothing to remove — navigate to or set a song in the setlist"
    s.select_song("B")
    s.setlist_add()
    assert (s.setlist.songs, s.setlist_idx) == (["A", "B"], 1)
    s.setlist_remove()
    assert s.setlist.songs == ["A"]
    assert env.log[-1] == '− Removed "B" from setlist'
    s.song_name = "A"
    s.setlist_idx = -1
    s.setlist_remove()  # the current song, when not navigating
    assert s.setlist.songs == []


def test_file_errors_are_reported(tmp_path: Path) -> None:
    env = _env(tmp_path)
    s = env.session
    s.songs.path = tmp_path / "missing" / "songs.json"
    s.select_song("A")
    assert env.log[-1].startswith("Song store save failed:")
    s.config.setlist_path.mkdir()  # a directory can't be appended to
    s.setlist_add()
    assert env.log[-1].startswith("Setlist save failed:")


def test_metronome_controls(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    s = env.session
    s.toggle_metronome()
    assert s.metronome.active and s.metronome.out_device_name == "Fake DL32S"
    s.toggle_metronome()
    assert not s.metronome.active
    s.set_bpm(500)
    assert s.metronome.bpm == 300
    s.cycle_time_signature()
    assert s.metronome.beats_per_bar == 5
    s.toggle_subdivision()
    assert env.log[-1] == "Subdivision ON"

    def fail(device: object = None) -> None:
        raise MetronomeError("no output")

    monkeypatch.setattr(s.metronome, "start", fail)
    s.toggle_metronome()
    assert env.log[-1] == "no output"


def test_stems_toggle(tmp_path: Path) -> None:
    env = _env(tmp_path)
    env.session.toggle_stems()
    assert env.log[-1] == "No stems configured — see config.example.toml"

    env = _env(tmp_path, channels=STEMS)
    env.session.select_song("A")
    env.session.toggle_stems()
    assert env.log[-1] == "Stems ON"
    assert env.session.songs.get("A") == SongSettings(stems=True)
    env.session.audio.stems_unavailable = "device has only 1 input(s)"
    env.session.toggle_stems()
    env.session.toggle_stems()
    assert env.log[-1] == "Stems ON (unavailable: device has only 1 input(s))"


# ─── TUI ──────────────────────────────────────────────────────────────────────


def _screen(tui: Tui) -> str:
    return "\n".join("".join(text for text, _ in row) for row in tui.render())


def test_render_idle_screen(env: Env) -> None:
    screen = _screen(Tui(env.session))
    assert "Input device:  " in screen
    assert "Song name: (none — press n to set)" in screen
    assert "Metronome: ○ OFF  120 BPM  4/4" in screen
    assert "Recording: ■ stopped" in screen
    assert "Stems:" not in screen


def test_render_while_recording_and_rating(tmp_path: Path) -> None:
    env = _env(tmp_path, channels=STEMS, setlist="")
    tui = Tui(env.session)
    env.session.toggle_stems()
    env.session.toggle_recording()
    env.clock.t += 65
    assert "Recording: ● 01:05  2026-10-08_20-14-03" in _screen(tui)
    env.session.toggle_recording()
    screen = _screen(tui)
    assert 'Rate take "2026-10-08_20-14-03":  [1]–[5]   [0/Esc] skip' in screen
    assert "Stems:     ● ON  1 ch, upload all" in screen
    env.session.audio.stems_unavailable = "device has only 1 input(s)"
    assert "Stems:     ● ON — unavailable: device has only 1 input(s)" in _screen(tui)
    env.session.toggle_stems()
    assert "Stems:     ○ OFF" in _screen(tui)
    env.session.shutdown()


def test_meter_row_colours() -> None:
    from taperoll.audio import LevelMeter

    assert meter_row("L", LevelMeter(level=1.0, hold=1.0))[1][1] is Style.LEVEL_CLIP
    assert meter_row("L", LevelMeter(level=0.2, hold=1.0))[1][1] is Style.LEVEL_HOT
    row = meter_row("L", LevelMeter(level=0.01, hold=0.5))
    assert row[1][1] is Style.LEVEL_OK
    assert row[2][0].startswith("▒")  # gap up to the held peak
    assert row[-1][0] == " -40.0 dB"


def test_keys_name_prompt(tmp_path: Path) -> None:
    env = _env(
        tmp_path, songs={"Caballo Viejo": SongSettings(82), "Circles": SongSettings()}
    )
    tui = Tui(env.session)
    for key in ["n", "c", "a"]:
        tui.handle_key(key)
    assert isinstance(tui.prompt, NamePrompt)
    assert tui.prompt.suggestions == ["Caballo Viejo"]
    assert "› " not in _screen(tui)
    tui.handle_key("KEY_DOWN")
    tui.handle_key("KEY_DOWN")  # only one suggestion
    tui.handle_key("x")  # typing is ignored while a suggestion is selected
    assert "  › Caballo Viejo  (82 BPM  4/4)" in _screen(tui)
    tui.handle_key("\n")
    assert (tui.prompt, env.session.song_name, env.session.metronome.bpm) == (
        None,
        "Caballo Viejo",
        82,
    )

    tui.handle_key("n")
    tui.handle_key("KEY_DOWN")
    tui.handle_key("KEY_UP")
    tui.handle_key("KEY_BACKSPACE")
    assert "Song name: Caballo Viej_" in _screen(tui)
    tui.handle_key("\x1b")
    assert tui.prompt is None and env.session.song_name == "Caballo Viejo"


def test_keys_bpm_prompt(env: Env) -> None:
    tui = Tui(env.session)
    tui.handle_key("b")
    for key in ["\x7f", "\x7f", "\x7f", "9", "a", "5"]:
        tui.handle_key(key)
    assert isinstance(tui.prompt, BpmPrompt)
    assert "BPM: 95_" in _screen(tui)
    tui.handle_key("\r")
    assert env.session.metronome.bpm == 95
    tui.handle_key("b")
    tui.handle_key("\x1b")
    tui.handle_key("b")
    for _ in range(3):
        tui.handle_key("\x7f")
    tui.handle_key("\n")  # empty: unchanged
    assert env.session.metronome.bpm == 95


def test_keys_commands_and_quit(env: Env) -> None:
    tui = Tui(env.session)
    assert tui.handle_key("r")
    assert tui.handle_key("q")  # can't quit while recording
    assert tui.handle_key("n") and tui.prompt is None  # no renaming while recording
    env.clock.t += 20
    tui.handle_key("r")
    tui.handle_key("3")
    assert env.log[-1].startswith("★★★☆☆")
    tui.handle_key("r")
    env.clock.t += 20
    tui.handle_key("r")
    tui.handle_key("0")
    assert env.log[-1].startswith("Rating skipped")
    for key in ["m", "t", "s", "x", "KEY_DOWN", "KEY_UP", "a", "d", "?"]:
        assert tui.handle_key(key)
    assert env.session.metronome.active
    env.session.metronome.stop()
    assert not tui.handle_key("q")

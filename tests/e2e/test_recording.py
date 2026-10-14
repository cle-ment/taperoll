"""Recording and rating takes through the real TUI."""

from __future__ import annotations

import datetime
import time

import pytest

from .conftest import App


def _saved(app: App, pattern: str = "*.mp3") -> list[str]:
    return sorted(p.name for p in app.recordings.glob(pattern))


def test_startup_screen_and_quit(app: App) -> None:
    tui = app.start()
    tui.wait_for("Input device:  Fake DL32S")
    tui.wait_for("Output device: (metronome off)")
    tui.wait_for("Song name: (none — press n to set)")
    tui.wait_for("Recording: ■ stopped")
    assert " L █" in tui.text and " R █" in tui.text
    assert tui.quit() == 0


def test_rated_take_is_saved_with_rating(app: App) -> None:
    tui = app.start()
    tui.press("n")
    tui.type("Circles")
    tui.press("enter")
    tui.wait_for("Song name: Circles")
    tui.record()
    tui.wait_for('Rate take "circles_')
    tui.press("4")
    tui.wait_for("★★★★☆  circles_")
    tui.wait_until_gone("Rate take")
    assert tui.quit() == 0

    (name,) = _saved(app)
    assert name.startswith(f"circles_{datetime.date.today().isoformat()}_")
    assert name.endswith("_rating=4.mp3")
    assert app.songs()["Circles"]["bpm"] == 120


@pytest.mark.parametrize("key", ["0", "esc"])
def test_skipping_the_rating_keeps_the_plain_name(app: App, key: str) -> None:
    tui = app.start()
    tui.record()
    tui.wait_for("Rate take")
    tui.press(key)
    tui.wait_for("Rating skipped")
    assert tui.quit() == 0
    (name,) = _saved(app)
    assert "rating" not in name


def test_starting_the_next_take_skips_the_previous_rating(app: App) -> None:
    tui = app.start()
    tui.record()
    tui.wait_for("Rate take")
    tui.press("r")
    tui.wait_for("Rating skipped")
    time.sleep(1.5)
    tui.press("r")
    tui.wait_for("Rate take")
    tui.press("5")
    assert tui.quit() == 0
    names = _saved(app)
    assert len(names) == 2
    assert sum(n.endswith("_rating=5.mp3") for n in names) == 1
    assert sum("rating" not in n for n in names) == 1


def test_quitting_keeps_an_unrated_pending_take_unrated(app: App) -> None:
    tui = app.start()
    tui.record()
    tui.wait_for("Rate take")
    assert tui.quit() == 0
    (name,) = _saved(app)
    assert "rating" not in name


def test_too_short_take_is_discarded(app: App) -> None:
    tui = app.start(min_recording_seconds=5)
    tui.record(seconds=0.5)
    tui.wait_for("Cancelled")
    assert "Rate take" not in tui.text
    assert tui.quit() == 0
    assert not list(app.recordings.glob("*"))


def test_quit_is_ignored_while_recording(app: App) -> None:
    tui = app.start()
    tui.press("r")
    tui.wait_for("Recording: ●")
    tui.press("q")
    time.sleep(1.2)  # also gets past the 1 s minimum take length
    assert tui.child.isalive()
    tui.press("r")
    tui.wait_for("Rate take")
    assert tui.quit() == 0


def test_missing_device_falls_back_to_system_default(app: App) -> None:
    tui = app.start(audio_device="Not Plugged In")
    tui.wait_for("Input device:  Laptop Mic")
    assert " M █" in tui.text  # mono meter
    tui.record()
    tui.press("1")
    assert tui.quit() == 0
    (name,) = _saved(app)
    assert name.endswith("_rating=1.mp3")

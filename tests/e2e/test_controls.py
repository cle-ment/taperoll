"""Song naming, metronome controls and setlist navigation through the real TUI."""

from __future__ import annotations

from .conftest import App


def test_metronome_bpm_signature_and_subdivision(app: App) -> None:
    tui = app.start()
    tui.press("n")
    tui.type("Dare")
    tui.press("enter")

    tui.press("m")
    tui.wait_for("Metronome: ♩ ON")
    tui.wait_for("Output device: Fake DL32S")

    tui.press("b")
    tui.wait_for("BPM: 120_")
    tui.press("backspace", "backspace", "backspace")
    tui.type("95")
    tui.press("enter")
    tui.wait_for("95 BPM")

    tui.press("t")
    tui.wait_for("95 BPM  5/4")
    tui.press("s")
    tui.wait_for("5/4  sub")
    tui.wait_for("Subdivision ON")

    tui.press("m")
    tui.wait_for("Metronome: ○ OFF")
    assert tui.quit() == 0
    assert app.songs()["Dare"] == {"bpm": 95, "sig": 5, "stems": False}


def test_bpm_entry_is_clamped_and_escape_cancels(app: App) -> None:
    tui = app.start()
    tui.press("b", "backspace", "backspace", "backspace")
    tui.type("999")
    tui.press("enter")
    tui.wait_for("300 BPM")
    tui.press("b")
    tui.type("5")
    tui.press("esc")
    tui.wait_for("300 BPM")
    assert tui.quit() == 0


def test_song_name_suggestions(app: App) -> None:
    app.write_songs(
        {
            "Caballo Viejo": {"bpm": 82, "sig": 4},
            "Circles": {"bpm": 120, "sig": 3},
        }
    )
    tui = app.start()
    tui.press("n")
    tui.type("cab")
    tui.wait_for("Caballo Viejo  (82 BPM  4/4)")
    tui.press("down", "enter")
    tui.wait_for("Song name: Caballo Viejo")
    tui.wait_for("82 BPM  4/4")

    # Escape keeps the previous name; backspace edits the buffer.
    tui.press("n")
    tui.type("xx")
    tui.press("backspace", "esc")
    tui.wait_for("Song name: Caballo Viejo")

    # A new name is stored with the current settings.
    tui.press("n")
    for _ in "Caballo Viejo":
        tui.press("backspace")
    tui.type("New One")
    tui.press("enter")
    tui.wait_for("Song name: New One")
    assert tui.quit() == 0
    assert app.songs()["New One"]["bpm"] == 82


def test_setlist_navigation_add_and_remove(app: App) -> None:
    app.write_songs(
        {
            "Arrete": {"bpm": 126, "sig": 4},
            "Dare": {"bpm": 105, "sig": 3},
            "Circles": {"bpm": 120, "sig": 4},
        }
    )
    setlist = app.home / "setlist.txt"
    setlist.write_text("# Set 1\nArrete\ndare\nBohemian Rhapsody\n")
    tui = app.start()
    tui.wait_for('"Bohemian Rhapsody" → no match')
    tui.wait_for("Setlist loaded: 2 songs from setlist.txt")
    tui.wait_for("Setlist:   2 songs loaded  ↓ to start")

    tui.press("down")
    tui.wait_for("→ [1/2] Arrete  126 BPM  4/4")
    tui.wait_for("Setlist:   [1/2]")
    tui.press("down")
    tui.wait_for("Song name: Dare")
    tui.wait_for("105 BPM  3/4")
    tui.press("up")
    tui.wait_for("Song name: Arrete")

    tui.press("d")
    tui.wait_for('− Removed "Arrete" from setlist')
    assert "Arrete" not in setlist.read_text()

    tui.press("n")
    for _ in "Arrete":
        tui.press("backspace")  # the prompt starts with the current name
    tui.type("Circles")
    tui.press("enter")
    tui.press("a")
    tui.wait_for('+ Added "Circles" to setlist')
    assert setlist.read_text().splitlines()[-1] == "Circles"
    assert tui.quit() == 0


def test_setlist_needs_a_song_name_to_add(app: App) -> None:
    tui = app.start()
    tui.press("a")
    tui.wait_for("No song name set — press n first")
    tui.press("d")
    tui.wait_for("Nothing to remove")
    assert tui.quit() == 0

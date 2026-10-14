"""Stems recording through the real TUI, with a fake 8-input interface."""

from __future__ import annotations

from .conftest import App

CHANNELS = """
[channels]
mixdown = [1, 2]

[[channels.stem]]
channel = 3
name = "kick"

[[channels.stem]]
channel = 4
name = "bass"

[[channels.stem]]
channel = 5
name = "guitar"

[[channels.stem]]
channel = 8
name = "spare"
"""


def _record_stems_take(app: App, rating: str) -> None:
    app.settings = {"config_toml": CHANNELS, "silent_channels": [8]}
    tui = app.start()
    tui.press("n")
    tui.type("Circles")
    tui.press("enter")
    tui.wait_for("Stems:     ○ OFF")
    tui.press("x")
    tui.wait_for("Stems:     ● ON  4 ch, upload all")
    tui.record()
    tui.wait_for("Stems  3 saved, 1 silent dropped")
    tui.press(rating)
    assert tui.quit() == 0


def test_stems_are_saved_next_to_the_mixdown(app: App) -> None:
    _record_stems_take(app, "5")
    (folder,) = app.recordings.glob("circles_*_rating=5_stems")
    take = folder.name.removesuffix("_rating=5_stems")
    assert sorted(p.name for p in folder.iterdir()) == [
        f"{take}_03_kick.flac",
        f"{take}_04_bass.flac",
        f"{take}_05_guitar.flac",
    ]
    assert (app.recordings / f"{take}_rating=5.mp3").exists()
    assert app.songs()["Circles"]["stems"] is True


def test_stems_mode_is_restored_per_song(app: App) -> None:
    app.write_songs(
        {
            "Circles": {"bpm": 120, "sig": 4, "stems": True},
            "Dare": {"bpm": 105, "sig": 4},
        }
    )
    (app.home / "setlist.txt").write_text("Circles\nDare\n")
    tui = app.start(config_toml=CHANNELS)
    tui.press("down")
    tui.wait_for("→ [1/2] Circles  120 BPM  4/4  stems")
    tui.wait_for("Stems:     ● ON")
    tui.press("down")
    tui.wait_for("Stems:     ○ OFF")
    assert tui.quit() == 0


def test_stems_unavailable_on_a_smaller_device(app: App) -> None:
    tui = app.start(config_toml=CHANNELS, audio_device="Laptop Mic")
    tui.press("x")
    tui.wait_for("Stems ON (unavailable: device has only 1 input(s))")
    tui.wait_for("Stems:     ● ON — unavailable")
    tui.record()
    tui.press("5")
    assert tui.quit() == 0
    assert len(list(app.recordings.glob("*_rating=5.mp3"))) == 1
    assert not list(app.recordings.glob("*_stems"))


def test_without_stems_config_x_explains(app: App) -> None:
    tui = app.start()
    tui.press("x")
    tui.wait_for("No stems configured")
    assert "Stems:" not in tui.text
    assert tui.quit() == 0


def test_invalid_config_file_is_reported(app: App) -> None:
    tui = app.start(config_toml="[channels]\nmixdown = [1, 2\n")
    tui.wait_for("⚠ config.toml is invalid")
    tui.wait_for("using defaults")
    assert tui.quit() == 0


def test_invalid_channel_map_disables_stems(app: App) -> None:
    tui = app.start(
        config_toml='[channels]\nmixdown = [1, 2]\n[[channels.stem]]\nchannel = 2\nname = "x"\n'
    )
    tui.wait_for("⚠ config.toml: [channels] a channel is listed twice — stems disabled")
    tui.press("x")
    tui.wait_for("No stems configured")
    assert tui.quit() == 0


def test_invalid_setting_is_reported_and_ignored(app: App) -> None:
    tui = app.start(config_toml='[metronome]\nbpm_default = "fast"\n[typo]\n')
    tui.wait_for("metronome.bpm_default = 'fast' is invalid — using the default")
    tui.wait_for("unknown section [typo] ignored")
    tui.wait_for("120 BPM")
    assert tui.quit() == 0

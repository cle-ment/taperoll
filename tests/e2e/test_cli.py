"""Non-interactive commands: --list-devices and --upload."""

from __future__ import annotations

from .conftest import App


def test_list_devices(app: App) -> None:
    output = app.run_cli("--list-devices")
    assert "[ 0]  Laptop Mic  (1 ch) ← default" in output
    assert "[ 1]  Fake DL32S  (8 ch)" in output


def test_upload_pending_recordings(app: App) -> None:
    rec = app.recordings
    rec.mkdir()
    (rec / "circles_2026-10-01_20-00-00_rating=4.mp3").write_bytes(b"mp3")
    (rec / "dare_2026-10-01_21-00-00.mp3").write_bytes(b"mp3")
    rated = rec / "circles_2026-10-01_20-00-00_rating=4_stems"
    rated.mkdir()
    (rated / "kick.flac").write_bytes(b"flac")
    unrated = rec / "dare_2026-10-01_21-00-00_stems"
    unrated.mkdir()
    (unrated / "kick.flac").write_bytes(b"flac")
    (rec / "crashed_2026-10-01_22-00-00_stems.partial").mkdir()

    output = app.run_cli("--upload", config_toml="[stems]\nupload_min_rating = 4\n")

    remote = app.home / "remote" / "2026-10-01"
    assert sorted(p.name for p in remote.iterdir()) == [
        "circles_2026-10-01_20-00-00_rating=4.mp3",
        "circles_2026-10-01_20-00-00_rating=4_stems",
        "dare_2026-10-01_21-00-00.mp3",
    ]
    assert (remote / rated.name / "kick.flac").exists()
    assert "Done: 3 uploaded, 0 failed." in output
    assert "1 stems folder(s) kept locally" in output
    assert unrated.exists()


def test_upload_with_nothing_to_do(app: App) -> None:
    app.recordings.mkdir()
    assert "No .mp3 files or rated stems found" in app.run_cli("--upload")

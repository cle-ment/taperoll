"""The take pipeline (save → encode → stems → rating) and the Uploader.

Uses the real ffmpeg and rclone from the pixi environment; the upload
"remote" is a local folder.
"""

from __future__ import annotations

import subprocess
import threading
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from taperoll import upload as upload_module
from taperoll.activity import ActivityLog
from taperoll.audio import StemWriter
from taperoll.config import (
    Config,
    RecordingConfig,
    Stem,
    StemsConfig,
    UploadConfig,
)
from taperoll.takes import RatingSlot, Take, TakeProcessor, normalize
from taperoll.upload import Uploader, upload_pending

TS = "2026-10-08_20-14-03"
TAKE = "circles_2026-10-08_20-14-03"
STEMS = (Stem(3, "kick"), Stem(4, "bass"))


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(
        home=tmp_path,
        recording=RecordingConfig(encode_mp3=False),
        upload=UploadConfig(destination=f"{tmp_path / 'remote'}/"),
    )


@pytest.fixture
def rclone_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    """Record every rclone invocation (still running the real rclone)."""
    calls: list[tuple[str, ...]] = []
    real = upload_module._rclone

    def spy(*args: str) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return real(*args)

    monkeypatch.setattr(upload_module, "_rclone", spy)
    return calls


def _remote(config: Config) -> Path:
    return config.home / "remote" / "2026-10-08"


def _frames(seconds: int = 1) -> list[np.ndarray]:
    return [np.full((48000, 2), 1000, dtype=np.int32)] * seconds


def _stem_writer(config: Config, loud: tuple[bool, ...] = (True, True)) -> StemWriter:
    writer = StemWriter(config.output_dir / f"{TAKE}_stems.partial", STEMS, TAKE, 48000)
    block = np.zeros((48000, len(STEMS)), dtype=np.int32)
    for i, is_loud in enumerate(loud):
        block[:, i] = (1 << 28) if is_loud else 0
    writer.put(block)
    return writer


def _process(
    config: Config, rating: int | None, writer: StemWriter | None = None
) -> ActivityLog:
    take = Take(ts=TS, song_name="Circles", frames=_frames(3), stem_writer=writer)
    take.rating.resolve(rating)
    activity = ActivityLog(maxlen=20)
    TakeProcessor(config, activity).process(take)
    return activity


# ─── building blocks ──────────────────────────────────────────────────────────


def test_rating_slot_first_resolve_wins_and_unblocks() -> None:
    slot = RatingSlot()
    result: list[int | None] = []
    t = threading.Thread(target=lambda: result.append(slot.wait()))
    t.start()
    slot.resolve(2)
    slot.resolve(5)
    t.join(timeout=1)
    assert result == [2]


def test_normalize_only_scales_up() -> None:
    quiet = np.array([[1000], [-500]], dtype=np.int16)
    loud = normalize(quiet, -6.0)
    assert int(loud.max()) == pytest.approx(32767 * 10 ** (-6 / 20), abs=1)
    assert normalize(loud, -20.0) is loud
    silent = np.zeros((4, 1), dtype=np.int16)
    assert normalize(silent, -1.0) is silent


# ─── TakeProcessor ────────────────────────────────────────────────────────────


def _recordings(config: Config) -> list[str]:
    return sorted(p.name for p in config.output_dir.iterdir())


def test_rated_take_is_saved_with_its_rating(config: Config) -> None:
    activity = _process(config, 4)
    assert _recordings(config) == [f"{TAKE}_rating=4.wav"]
    lines = activity.lines()
    assert lines[0].startswith(f"Saved  {TAKE}.wav  3s / ")
    assert lines[-1] == "Ready ✓"


def test_unrated_take_keeps_its_plain_name(config: Config) -> None:
    _process(config, None)
    assert _recordings(config) == [f"{TAKE}.wav"]


def test_mp3_encoding(config: Config) -> None:
    config = replace(config, recording=RecordingConfig())
    activity = _process(config, 3)
    assert _recordings(config) == [f"{TAKE}_rating=3.mp3"]
    assert f"Encoded  {TAKE}.mp3" in activity.lines()


def test_stems_are_saved_as_flac_next_to_the_mixdown(config: Config) -> None:
    activity = _process(config, 5, _stem_writer(config))
    folder = config.output_dir / f"{TAKE}_rating=5_stems"
    assert sorted(p.name for p in folder.iterdir()) == [
        f"{TAKE}_03_kick.flac",
        f"{TAKE}_04_bass.flac",
    ]
    assert "Stems  2 saved" in activity.lines()


def test_stems_can_be_encoded_as_mp3(config: Config) -> None:
    config = replace(config, stems=StemsConfig(format="mp3"))
    _process(config, 5, _stem_writer(config))
    folder = config.output_dir / f"{TAKE}_rating=5_stems"
    assert sorted(p.name for p in folder.iterdir()) == [
        f"{TAKE}_03_kick.mp3",
        f"{TAKE}_04_bass.mp3",
    ]


def test_silent_stems_are_dropped(config: Config) -> None:
    activity = _process(config, 3, _stem_writer(config, loud=(True, False)))
    folder = config.output_dir / f"{TAKE}_rating=3_stems"
    assert [p.name for p in folder.iterdir()] == [f"{TAKE}_03_kick.flac"]
    assert "Stems  1 saved, 1 silent dropped" in activity.lines()


def test_all_silent_stems_leave_no_folder(config: Config) -> None:
    _process(config, 5, _stem_writer(config, loud=(False, False)))
    assert not list(config.output_dir.glob("*_stems*"))


def test_stems_stay_wav_without_ffmpeg(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("taperoll.takes.shutil.which", lambda _: None)
    _process(config, 2, _stem_writer(config))
    local = config.output_dir / f"{TAKE}_rating=2_stems"
    assert sorted(p.suffix for p in local.iterdir()) == [".wav", ".wav"]


# ─── Uploader ─────────────────────────────────────────────────────────────────


def _stems_folder(config: Config) -> Path:
    folder = config.output_dir / f"{TAKE}_rating=5_stems"
    folder.mkdir(parents=True)
    for name in ("a.flac", "b.flac"):
        (folder / name).write_bytes(b"x")
    return folder


def test_upload_stems_copies_each_file_and_deletes_the_folder(
    config: Config, rclone_calls: list[tuple[str, ...]]
) -> None:
    uploader = Uploader(config)
    folder = _stems_folder(config)
    result = uploader.upload_stems(folder, uploader.dest_dir("2026-10-08"))
    assert result.ok
    assert len(rclone_calls) == 2  # one call per file
    assert sorted(p.name for p in (_remote(config) / folder.name).iterdir()) == [
        "a.flac",
        "b.flac",
    ]
    assert not folder.exists()


@pytest.mark.parametrize(
    ("upload", "message"),
    [
        (UploadConfig(enabled=False, destination="r:"), "Upload disabled"),
        (UploadConfig(destination=""), "no upload.destination configured"),
    ],
)
def test_unavailable_reasons(
    tmp_path: Path, upload: UploadConfig, message: str
) -> None:
    reason = Uploader(Config(home=tmp_path, upload=upload)).unavailable()
    assert reason is not None
    assert message in reason


def test_unavailable_without_rclone(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("taperoll.upload.shutil.which", lambda _: None)
    assert "rclone not found" in (Uploader(config).unavailable() or "")


def test_failed_upload_keeps_the_file(config: Config) -> None:
    missing = config.home / "missing.mp3"
    result = Uploader(config).upload_file(missing, "/dev/null/nope")
    assert (result.ok, result.message) == (False, "Upload FAILED — file kept locally")


def test_notify_command_runs_after_upload(config: Config) -> None:
    marker = config.home / "notified"
    config = replace(
        config,
        upload=replace(
            config.upload, delete_after_upload=False, notify_command=f"touch {marker}"
        ),
    )
    mp3 = config.home / "a.mp3"
    mp3.touch()
    assert Uploader(config).upload_file(mp3, f"{config.home}/remote/d").ok
    assert marker.exists() and mp3.exists()


def test_share_links_are_skipped_for_local_remotes(config: Config) -> None:
    uploader = Uploader(config)
    uploader.uploaded_dirs.add(str(config.home / "remote" / "d"))
    assert uploader.share_links() == []  # `rclone link` fails for local paths


def test_upload_pending_reports(config: Config) -> None:
    config.output_dir.mkdir()
    (config.output_dir / f"{TAKE}.wav").touch()
    lines: list[str] = []
    upload_pending(config, Uploader(config), lines.append)
    assert lines[-1] == "\nDone: 1 uploaded, 0 failed."
    assert (_remote(config) / f"{TAKE}.wav").exists()


def test_upload_pending_stops_early_when_upload_cannot_work(tmp_path: Path) -> None:
    config = Config(home=tmp_path, upload=UploadConfig(enabled=False))
    config.output_dir.mkdir()
    (config.output_dir / f"{TAKE}.mp3").touch()
    lines: list[str] = []
    upload_pending(config, Uploader(config), lines.append)
    assert lines == ["Upload disabled"]


def test_upload_pending_shares_mixdown_links_before_stems(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    config.output_dir.mkdir()
    (config.output_dir / f"{TAKE}.wav").touch()
    folder = config.output_dir / f"{TAKE}_stems"
    folder.mkdir()
    (folder / "a.flac").write_bytes(b"x")
    monkeypatch.setattr(
        Uploader, "share_links", lambda self: [("2026-10-08", "https://link")]
    )
    lines: list[str] = []
    upload_pending(config, Uploader(config), lines.append)
    text = "\n".join(lines)
    assert text.count("https://link") == 1
    assert text.index("https://link") < text.index(f"{folder.name}  →")
    assert text.index("Uploading mixdowns…") < text.index("https://link")
    assert text.index("https://link") < text.index("Uploading stems…")

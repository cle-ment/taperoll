"""Loading config.toml, and the file naming rules."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from taperoll import naming
from taperoll.config import (
    HOME_ENV,
    ChannelConfig,
    Config,
    Stem,
    default_home,
    load_config,
)

TS = "2026-10-08_20-14-03"


def _load(tmp_path: Path, text: str) -> tuple[Config, list[str]]:
    (tmp_path / "config.toml").write_text(text)
    return load_config(tmp_path)


# ─── config ───────────────────────────────────────────────────────────────────


def test_without_config_file_everything_is_default(tmp_path: Path) -> None:
    config, warnings = load_config(tmp_path)
    assert config == Config(home=tmp_path)
    assert warnings == []
    assert config.output_dir == tmp_path / "recordings"
    assert config.songs_path == tmp_path / "songs.json"
    assert config.setlist_path == tmp_path / "setlist.txt"
    assert config.log_path == tmp_path / "taperoll.log"


def test_values_override_defaults(tmp_path: Path) -> None:
    config, warnings = _load(
        tmp_path,
        """
        [audio]
        device = "DL32S"
        [recording]
        min_seconds = 5
        normalize_target_db = -3
        [upload]
        destination = "remote:Rehearsals/"
        notify_command = "say {file}"
        [metronome]
        time_sigs = [3, 4]
        [stems]
        upload_min_rating = 5
        [channels]
        mixdown = [1, 2]
        [[channels.stem]]
        channel = 3
        name = "kick"
        """,
    )
    assert warnings == []
    assert config.audio.device == "DL32S"
    assert config.recording.min_seconds == 5
    assert config.recording.normalize_target_db == -3  # int accepted for float
    assert config.upload.destination == "remote:Rehearsals/"
    assert config.upload.notify_command == "say {file}"
    assert config.metronome.time_sigs == (3, 4)
    assert config.stems.upload_min_rating == 5
    assert config.channels == ChannelConfig(mixdown=(1, 2), stems=(Stem(3, "kick"),))
    assert config.channels.max_channel == 3


def test_device_may_be_an_index(tmp_path: Path) -> None:
    config, warnings = _load(tmp_path, "[audio]\ndevice = 3\n")
    assert (config.audio.device, warnings) == (3, [])


@pytest.mark.parametrize(
    ("text", "warning"),
    [
        ('[audio]\nsample_rate = "fast"\n', "audio.sample_rate = 'fast' is invalid"),
        ("[audio]\ndtype = 'float64'\n", "audio.dtype = 'float64' is invalid"),
        ("[upload]\nenabled = 1\n", "upload.enabled = 1 is invalid"),
        ("[stems]\nformat = 'ogg'\n", "stems.format = 'ogg' is invalid"),
        (
            "[recording]\nmin_seconds = true\n",
            "recording.min_seconds = True is invalid",
        ),
        (
            "[metronome]\ntime_sigs = [3, 'x']\n",
            "metronome.time_sigs = [3, 'x'] is invalid",
        ),
        ("[audio]\nchannels = 2\n", "unknown setting audio.channels ignored"),
        ("[typo]\n", "unknown section [typo] ignored"),
        ("audio = 3\n", "[audio] must be a table"),
    ],
)
def test_invalid_values_warn_and_fall_back(
    tmp_path: Path, text: str, warning: str
) -> None:
    config, warnings = _load(tmp_path, text)
    assert len(warnings) == 1
    assert warning in warnings[0]
    assert config.audio == Config().audio  # nothing else changed


def test_invalid_toml_uses_defaults(tmp_path: Path) -> None:
    config, warnings = _load(tmp_path, "[audio\n")
    assert config == Config(home=tmp_path)
    assert "config.toml is invalid" in warnings[0]


@pytest.mark.parametrize(
    ("channels", "reason"),
    [
        ("mixdown = [1, 2, 3]", "mixdown needs 1 or 2 channels"),
        ("mixdown = [0, 1]", "channel numbers start at 1"),
        (
            'mixdown = [1, 2]\n[[channels.stem]]\nchannel = 2\nname = "kick"',
            "listed twice",
        ),
        ('[[channels.stem]]\nchannel = 3\nname = "!!"', "every stem needs a name"),
        ("[[channels.stem]]\nchannel = 3", "is invalid"),
        ('mixdown = ["a"]', "is invalid"),
    ],
)
def test_invalid_channel_maps_disable_stems(
    tmp_path: Path, channels: str, reason: str
) -> None:
    config, warnings = _load(tmp_path, f"[channels]\n{channels}\n")
    assert config.channels == ChannelConfig()
    assert reason in warnings[0]
    assert warnings[0].endswith("stems disabled")


def test_home_defaults_to_project_and_env_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(HOME_ENV, raising=False)
    assert (default_home() / "pyproject.toml").exists()
    monkeypatch.setenv(HOME_ENV, str(tmp_path))
    assert default_home() == tmp_path
    assert load_config()[0].home == tmp_path


# ─── naming ───────────────────────────────────────────────────────────────────


def test_sanitize_name() -> None:
    assert naming.sanitize_name("  Arrête Mal Parlé! ") == "arrête-mal-parlé"


def test_take_stem() -> None:
    assert naming.take_stem(TS, "Caballo Viejo") == "caballo-viejo_2026-10-08_20-14-03"
    assert naming.take_stem(TS) == TS


def test_rating_and_stems_names(tmp_path: Path) -> None:
    mp3 = tmp_path / "circles_2026-10-08_20-14-03.mp3"
    rated = naming.with_rating(mp3, 4)
    assert rated.name == "circles_2026-10-08_20-14-03_rating=4.mp3"
    assert (
        naming.stems_dir_for(rated).name == "circles_2026-10-08_20-14-03_rating=4_stems"
    )
    assert naming.stems_rating(naming.stems_dir_for(rated)) == 4
    assert naming.stems_rating(naming.stems_dir_for(mp3)) is None


def test_stem_filename() -> None:
    assert naming.stem_filename("take", 3, "Bass DI") == "take_03_bass-di.wav"


def test_recording_date_from_name_or_mtime(tmp_path: Path) -> None:
    dated = tmp_path / "x_2026-10-01_10-00-00.mp3"
    undated = tmp_path / "x.mp3"
    dated.touch()
    undated.touch()
    os.utime(undated, (1767225600, 1767225600))  # 2026-01-01 00:00 UTC
    assert naming.recording_date(dated) == "2026-10-01"
    assert naming.recording_date(undated) in ("2025-12-31", "2026-01-01")


def test_example_config_documents_the_defaults(tmp_path: Path) -> None:
    example = Path(__file__).parents[1] / "config.example.toml"
    config, warnings = _load(tmp_path, example.read_text())
    assert warnings == []
    assert config == Config(home=tmp_path)


@pytest.mark.parametrize(
    "text",
    [
        "[metronome]\ntime_sigs = []\n",
        "[metronome]\ntime_sigs = [0, 4]\n",
        "[metronome]\nbpm_default = 0\n",
        "[metronome]\nvolume = 1.5\n",
        "[audio]\nsample_rate = -48000\n",
        "[recording]\nmin_seconds = -1\n",
        "[stems]\nupload_min_rating = 6\n",
    ],
)
def test_out_of_range_values_fall_back(tmp_path: Path, text: str) -> None:
    config, warnings = _load(tmp_path, text)
    assert config == Config(home=tmp_path)
    assert "is invalid — using the default" in warnings[0]


def test_bpm_range_must_contain_the_default(tmp_path: Path) -> None:
    config, warnings = _load(tmp_path, "[metronome]\nbpm_min = 150\nvolume = 0.5\n")
    assert (config.metronome.bpm_min, config.metronome.volume) == (20, 0.5)
    assert "bpm_min ≤ bpm_default ≤ bpm_max" in warnings[0]


@pytest.mark.parametrize(
    ("channels", "reason"),
    [
        ('[[channels.stems]]\nchannel = 3\nname = "kick"', "unknown setting(s) stems"),
        (
            '[[channels.stem]]\nchannel = 3\nname = "kick"\ngain = 2',
            "only take channel and name",
        ),
    ],
)
def test_channel_typos_are_reported(tmp_path: Path, channels: str, reason: str) -> None:
    _, warnings = _load(tmp_path, f"[channels]\n{channels}\n")
    assert reason in warnings[0]


def test_channels_must_be_a_table(tmp_path: Path) -> None:
    _, warnings = _load(tmp_path, "channels = 3\n")
    assert "[channels] must be a table" in warnings[0]


def test_setlist_file_is_configurable(tmp_path: Path) -> None:
    (tmp_path / "setlists").mkdir()
    (tmp_path / "setlists" / "friday.txt").touch()
    config, warnings = _load(tmp_path, '[setlist]\nfile = "setlists/friday.txt"\n')
    assert warnings == []
    assert config.setlist_path == tmp_path / "setlists" / "friday.txt"


def test_missing_setlist_file_is_reported(tmp_path: Path) -> None:
    config, warnings = _load(tmp_path, '[setlist]\nfile = "nope.txt"\n')
    assert config.setlist_path == tmp_path / "nope.txt"
    assert warnings == [
        "config.toml: setlist.file 'nope.txt' not found — no setlist loaded"
    ]

"""Settings, loaded from ``config.toml`` in the app's home directory.

The home directory holds everything the recorder reads and writes: config.toml,
songs.json, setlist.txt, the log file and the recordings folder. It defaults to
the project folder and can be overridden with $TAPEROLL_HOME.

Every setting has a default, so config.toml only needs the values you change.
See config.example.toml for the full list.
"""

from __future__ import annotations

import dataclasses
import os
import tomllib
import types
import typing
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, TypeVar

from .naming import sanitize_name

if TYPE_CHECKING:
    from _typeshed import DataclassInstance

    _Section = TypeVar("_Section", bound=DataclassInstance)

HOME_ENV = "TAPEROLL_HOME"
CONFIG_FILE = "config.toml"


def default_home() -> Path:
    env = os.environ.get(HOME_ENV)
    return Path(env).expanduser() if env else Path(__file__).resolve().parent.parent


@dataclass(frozen=True, slots=True)
class AudioConfig:
    device: int | str | None = None  # input device index or name; None = system default
    sample_rate: int = 48000
    dtype: Literal["int16", "int32"] = "int32"  # int32 keeps stems at 24-bit


@dataclass(frozen=True, slots=True)
class RecordingConfig:
    output_dir: str = "recordings"  # relative to the home directory
    min_seconds: float = 10  # shorter takes are discarded
    normalize: bool = True  # boost the mixdown so its peak hits normalize_target_db
    normalize_target_db: float = -1.0  # only ever scales up, never down
    encode_mp3: bool = True  # requires ffmpeg
    mp3_bitrate: str = "192k"
    delete_wav_after_encode: bool = True


@dataclass(frozen=True, slots=True)
class UploadConfig:
    enabled: bool = True
    destination: str = ""  # rclone remote:path; empty = don't upload
    delete_after_upload: bool = True  # delete the local mixdown once uploaded
    notify_command: str | None = None  # run after each upload; {file} = file path


@dataclass(frozen=True, slots=True)
class MetronomeConfig:
    bpm_default: int = 120
    bpm_min: int = 20
    bpm_max: int = 300
    click_freq: int = 1000  # Hz
    click_freq_accent: int = 1800  # Hz, beat 1
    click_dur: float = 0.015  # seconds
    volume: float = 0.7  # 0.0–1.0
    subdivision_volume_mult: float = 0.3  # relative to volume
    time_sigs: tuple[int, ...] = (1, 3, 4, 5, 6)  # beats per bar to cycle through


@dataclass(frozen=True, slots=True)
class StemsConfig:
    upload_min_rating: int | None = None  # None: upload all stems
    format: Literal["flac", "mp3"] = "flac"  # mp3 uses recording.mp3_bitrate
    silence_db: float = -60.0  # stems whose peak stays below this are dropped
    delete_after_upload: bool = True

    def should_upload(self, rating: int | None) -> bool:
        """Whether stems of a take with this rating (None: unrated) get uploaded."""
        if self.upload_min_rating is None:
            return True
        return rating is not None and rating >= self.upload_min_rating


@dataclass(frozen=True, slots=True)
class SetlistConfig:
    file: str = "setlist.txt"  # relative to the home directory, or absolute


@dataclass(frozen=True, slots=True)
class Stem:
    channel: int  # 1-based input channel
    name: str


@dataclass(frozen=True, slots=True)
class ChannelConfig:
    mixdown: tuple[int, ...] = (1, 2)  # 1-based input channels: (L, R) or (mono,)
    stems: tuple[Stem, ...] = ()

    @property
    def max_channel(self) -> int:
        return max((*self.mixdown, *(s.channel for s in self.stems)))


@dataclass(frozen=True, slots=True)
class Config:
    home: Path = field(default_factory=default_home)
    audio: AudioConfig = AudioConfig()
    recording: RecordingConfig = RecordingConfig()
    upload: UploadConfig = UploadConfig()
    metronome: MetronomeConfig = MetronomeConfig()
    stems: StemsConfig = StemsConfig()
    setlist: SetlistConfig = SetlistConfig()
    channels: ChannelConfig = ChannelConfig()

    @property
    def output_dir(self) -> Path:
        return self.home / self.recording.output_dir

    @property
    def songs_path(self) -> Path:
        return self.home / "songs.json"

    @property
    def setlist_path(self) -> Path:
        return self.home / self.setlist.file

    @property
    def log_path(self) -> Path:
        return self.home / "taperoll.log"


def load_config(home: Path | None = None) -> tuple[Config, list[str]]:
    """Load home/config.toml. Returns the config and human-readable warnings.

    Invalid values are reported and replaced by their defaults, so a typo never
    keeps the recorder from starting.
    """
    home = home or default_home()
    path = home / CONFIG_FILE
    if not path.exists():
        return Config(home=home), []
    try:
        raw = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return Config(home=home), [f"{CONFIG_FILE} is invalid ({exc}) — using defaults"]

    warnings: list[str] = []

    def section(name: str, cls: type[_Section]) -> _Section:
        return _load_section(cls, raw.pop(name, {}), name, warnings)

    config = Config(
        home=home,
        audio=section("audio", AudioConfig),
        recording=section("recording", RecordingConfig),
        upload=section("upload", UploadConfig),
        metronome=section("metronome", MetronomeConfig),
        stems=section("stems", StemsConfig),
        setlist=section("setlist", SetlistConfig),
        channels=_load_channels(raw.pop("channels", {}), warnings),
    )
    warnings.extend(f"{CONFIG_FILE}: unknown section [{key}] ignored" for key in raw)
    if config.setlist != SetlistConfig() and not config.setlist_path.exists():
        warnings.append(
            f"{CONFIG_FILE}: setlist.file {config.setlist.file!r} not found — "
            "no setlist loaded"
        )
    return config, warnings


def _positive(v: float) -> bool:
    return v > 0


# Range checks on top of the type checks: values that would break the app.
_CHECKS: dict[str, Callable[[Any], bool]] = {
    "audio.sample_rate": _positive,
    "recording.min_seconds": lambda v: v >= 0,
    "metronome.bpm_default": _positive,
    "metronome.bpm_min": _positive,
    "metronome.bpm_max": _positive,
    "metronome.click_freq": _positive,
    "metronome.click_freq_accent": _positive,
    "metronome.click_dur": _positive,
    "metronome.volume": lambda v: 0 <= v <= 1,
    "metronome.subdivision_volume_mult": lambda v: v >= 0,
    "metronome.time_sigs": lambda v: len(v) > 0 and all(b >= 1 for b in v),
    "stems.upload_min_rating": lambda v: 1 <= v <= 5,
}


def _load_section(
    cls: type[_Section], table: Any, section: str, warnings: list[str]
) -> _Section:
    if not isinstance(table, dict):
        warnings.append(f"{CONFIG_FILE}: [{section}] must be a table — using defaults")
        return cls()
    hints = typing.get_type_hints(cls)
    known = {f.name for f in dataclasses.fields(cls)}
    values: dict[str, Any] = {}
    for key, value in table.items():
        if key not in known:
            warnings.append(f"{CONFIG_FILE}: unknown setting {section}.{key} ignored")
        elif not _matches(value, hints[key]) or not _CHECKS.get(
            f"{section}.{key}", lambda _: True
        )(value):
            warnings.append(
                f"{CONFIG_FILE}: {section}.{key} = {value!r} is invalid — using the default"
            )
        else:
            values[key] = tuple(value) if isinstance(value, list) else value
    result = cls(**values)
    if isinstance(result, MetronomeConfig) and not (
        result.bpm_min <= result.bpm_default <= result.bpm_max
    ):
        warnings.append(
            f"{CONFIG_FILE}: metronome needs bpm_min ≤ bpm_default ≤ bpm_max "
            "— using the default BPM range"
        )
        default = MetronomeConfig()
        result = dataclasses.replace(
            result,
            bpm_min=default.bpm_min,
            bpm_default=default.bpm_default,
            bpm_max=default.bpm_max,
        )
    return result


def _matches(value: Any, hint: Any) -> bool:
    """Whether a TOML value fits a (simple) type annotation."""
    origin = typing.get_origin(hint)
    if origin in (typing.Union, types.UnionType):
        return any(_matches(value, arg) for arg in typing.get_args(hint))
    if origin is Literal:
        return value in typing.get_args(hint)
    if origin is tuple:
        (item, _ellipsis) = typing.get_args(hint)
        return isinstance(value, list) and all(_matches(v, item) for v in value)
    if hint is float:
        return isinstance(value, int | float) and not isinstance(value, bool)
    if hint is int:
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, hint)


def _load_channels(table: Any, warnings: list[str]) -> ChannelConfig:
    def invalid(reason: str) -> ChannelConfig:
        warnings.append(f"{CONFIG_FILE}: [channels] {reason} — stems disabled")
        return ChannelConfig()

    if not table:
        return ChannelConfig()
    if not isinstance(table, dict):
        return invalid("must be a table")
    unknown = set(table) - {"mixdown", "stem"}
    if unknown:
        return invalid(f"has unknown setting(s) {', '.join(sorted(unknown))}")
    try:
        mixdown = tuple(int(c) for c in table.get("mixdown", ChannelConfig().mixdown))
        stems = tuple(
            Stem(int(s["channel"]), str(s["name"])) for s in table.get("stem", [])
        )
        if any(set(s) - {"channel", "name"} for s in table.get("stem", [])):
            return invalid("stems only take channel and name")
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        return invalid(f"is invalid ({exc})")
    channels = [*mixdown, *(s.channel for s in stems)]
    if not 1 <= len(mixdown) <= 2:
        return invalid("mixdown needs 1 or 2 channels")
    if any(ch < 1 for ch in channels):
        return invalid("channel numbers start at 1")
    if len(set(channels)) != len(channels):
        return invalid("a channel is listed twice")
    if any(not sanitize_name(s.name) for s in stems):
        return invalid("every stem needs a name")
    return ChannelConfig(mixdown=mixdown, stems=stems)

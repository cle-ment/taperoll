"""File and folder naming for takes.

A take recorded at 2026-10-08 20:14:03 for the song "Circles" is called
``circles_2026-10-08_20-14-03``. Rating it 4 appends ``_rating=4``; its stems
live in a sibling folder ``<take>_stems/``, each file prefixed with the take name.
"""

from __future__ import annotations

import datetime
import re
from pathlib import Path

TIMESTAMP_FORMAT = "%Y-%m-%d_%H-%M-%S"
STEMS_SUFFIX = "_stems"
PARTIAL_SUFFIX = ".partial"  # stems folder of a take that is still being recorded

_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_STEMS_RATING_RE = re.compile(r"_rating=(\d)_stems$")


def timestamp(now: datetime.datetime) -> str:
    return now.strftime(TIMESTAMP_FORMAT)


def sanitize_name(name: str) -> str:
    """Turn a freeform song name into a kebab-case, filename-safe string."""
    safe = "".join(c if c.isalnum() or c in " -_'" else "" for c in name.strip())
    return safe.strip().lower().replace(" ", "-")


def take_stem(ts: str, song_name: str = "") -> str:
    """Base filename (no extension) for a take: <song>_<timestamp>, or just the timestamp."""
    name_part = sanitize_name(song_name)
    return f"{name_part}_{ts}" if name_part else ts


def with_rating(path: Path, rating: int) -> Path:
    """Return path with a _rating=N suffix appended to the filename stem."""
    return path.with_name(f"{path.stem}_rating={rating}{path.suffix}")


def stems_dir_for(media_path: Path) -> Path:
    """The stems folder that belongs next to a mixdown file."""
    return media_path.with_name(f"{media_path.stem}{STEMS_SUFFIX}")


def stem_filename(take: str, channel: int, name: str) -> str:
    """WAV filename of one stem; the take prefix keeps it self-describing in a DAW."""
    return f"{take}_{channel:02d}_{sanitize_name(name)}.wav"


def stems_rating(folder: Path) -> int | None:
    """Rating encoded in a stems folder name, or None if the take is unrated."""
    m = _STEMS_RATING_RE.search(folder.name)
    return int(m.group(1)) if m else None


def recording_date(path: Path) -> str:
    """yyyy-mm-dd a recording belongs to: from its name, else its modification time."""
    m = _DATE_RE.search(path.name)
    if m:
        return m.group(1)
    return datetime.date.fromtimestamp(path.stat().st_mtime).isoformat()

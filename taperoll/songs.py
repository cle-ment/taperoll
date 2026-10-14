"""Per-song settings (songs.json) and fuzzy song-name matching."""

from __future__ import annotations

import difflib
import json
import logging
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, cast

log = logging.getLogger(__name__)


@dataclass(slots=True)
class SongSettings:
    bpm: int = 120
    sig: int = 4  # beats per bar
    stems: bool = False  # record stems for this song


class SongStore:
    """Song name → SongSettings, persisted as JSON."""

    def __init__(
        self, path: Path, songs: dict[str, SongSettings] | None = None
    ) -> None:
        self.path = path
        self._songs: dict[str, SongSettings] = songs or {}

    @classmethod
    def load(cls, path: Path) -> tuple[SongStore, str | None]:
        """Load from disk. Returns the store and a warning if the file is unreadable."""
        try:
            raw = json.loads(path.read_text())
            songs = {name: _parse(value) for name, value in raw.items()}
        except FileNotFoundError:
            return cls(path), None
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            log.warning("Could not load %s: %s", path, exc)
            return cls(path), f"Could not load {path.name}: {exc}"
        return cls(path, songs), None

    def __contains__(self, name: object) -> bool:
        return name in self._songs

    def __iter__(self) -> Iterator[str]:
        return iter(self._songs)

    def __len__(self) -> int:
        return len(self._songs)

    def get(self, name: str) -> SongSettings | None:
        return self._songs.get(name)

    def save(self, name: str, settings: SongSettings) -> None:
        """Store settings for a song and write the file. Raises OSError on failure."""
        self._songs[name] = settings
        data = {k: asdict(v) for k, v in self._songs.items()}
        self.path.write_text(json.dumps(data, indent=2, sort_keys=True))

    def suggestions(self, query: str, limit: int = 4) -> list[str]:
        """Song names matching query: prefix matches, then substring, then fuzzy."""
        if not query:
            return []
        q = query.lower()
        prefix = [k for k in self._songs if k.lower().startswith(q)]
        rest = [k for k in self._songs if k not in prefix]
        substr = [k for k in rest if q in k.lower()]
        fuzzy_pool = [k for k in rest if k not in substr]
        fuzzy = difflib.get_close_matches(q, fuzzy_pool, n=limit, cutoff=0.5)
        return [*prefix, *substr, *fuzzy][:limit]

    def resolve(self, query: str) -> str | None:
        """The one song query refers to: an exact (case-insensitive) or unique match."""
        for name in self._songs:
            if name.lower() == query.lower():
                return name
        matches = self.suggestions(query)
        return matches[0] if len(matches) == 1 else None


def _parse(value: object) -> SongSettings:
    if isinstance(value, dict):
        entry = cast("dict[str, Any]", value)
        return SongSettings(
            bpm=int(entry.get("bpm", 120)),
            sig=int(entry.get("sig", 4)),
            stems=bool(entry.get("stems", False)),
        )
    if isinstance(value, int | float | str):
        return SongSettings(bpm=int(value))  # old format stored just the BPM
    raise TypeError(f"unexpected song entry {value!r}")

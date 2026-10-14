"""setlist.txt: one song per line, '#' for comments, resolved against the song store."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .songs import SongStore


@dataclass(slots=True)
class Setlist:
    songs: list[str] = field(default_factory=list)  # exact song-store names, in order
    warnings: list[str] = field(default_factory=list)  # unresolvable lines

    def __len__(self) -> int:
        return len(self.songs)


def load_setlist(path: Path, store: SongStore) -> Setlist:
    setlist = Setlist()
    if not path.exists():
        return setlist
    for lineno, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name = store.resolve(line)
        if name is not None:
            setlist.songs.append(name)
        elif candidates := store.suggestions(line):
            setlist.warnings.append(
                f'Setlist line {lineno} "{line}" → ambiguous ({len(candidates)} matches), skipped'
            )
        else:
            setlist.warnings.append(
                f'Setlist line {lineno} "{line}" → no match in songs.json'
            )
    return setlist


def add_to_setlist(path: Path, name: str) -> None:
    """Append name unless it's already listed (case-insensitive). Raises OSError."""
    lines = path.read_text().splitlines() if path.exists() else []
    if any(line.strip().lower() == name.lower() for line in lines):
        return
    with path.open("a") as f:
        f.write(name + "\n")


def remove_from_setlist(path: Path, name: str) -> None:
    """Remove every line naming this song (case-insensitive). Raises OSError."""
    if not path.exists():
        return
    kept = [
        line
        for line in path.read_text().splitlines()
        if line.strip().lower() != name.lower()
    ]
    path.write_text("".join(f"{line}\n" for line in kept))

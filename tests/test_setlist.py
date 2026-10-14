"""Tests for resolving setlist lines against the song store, and loading setlist.txt."""

from pathlib import Path
from typing import Any

from taperoll.setlist import add_to_setlist, load_setlist, remove_from_setlist
from taperoll.songs import SongSettings, SongStore

# ─── helpers ──────────────────────────────────────────────────────────────────


def _store(songs: dict[str, dict[str, Any]]) -> SongStore:
    return SongStore(
        Path("songs.json"), {k: SongSettings(**v) for k, v in songs.items()}
    )


def _load(path: Path) -> tuple[list[str], list[str]]:
    setlist = load_setlist(path, STORE)
    return setlist.songs, setlist.warnings


STORE = _store(
    {
        "Highway to Hell": {"bpm": 116, "sig": 4},
        "Highway Star": {"bpm": 170, "sig": 4},
        "Smells Like Teen Spirit": {"bpm": 117, "sig": 4},
        "Paranoid": {"bpm": 164, "sig": 4},
        "Black Dog": {"bpm": 82, "sig": 4},
    }
)


# ─── resolve_setlist_entry ────────────────────────────────────────────────────


def test_exact_match_case_insensitive():
    result = STORE.resolve("highway to hell")
    assert result == "Highway to Hell"


def test_exact_match_preserves_original_key():
    """The resolved name must be the store key, not the setlist line."""
    result = STORE.resolve("PARANOID")
    assert result == "Paranoid"


def test_unique_prefix_match():
    """A prefix that uniquely identifies one song resolves to that song."""
    result = STORE.resolve("Smells")
    assert result == "Smells Like Teen Spirit"


def test_ambiguous_prefix_returns_none():
    """'Highway' prefixes two songs — must return None, not guess."""
    result = STORE.resolve("Highway")
    assert result is None


def test_no_match_returns_none():
    result = STORE.resolve("Bohemian Rhapsody")
    assert result is None


def test_exact_match_wins_over_ambiguous_prefix():
    """If the line exactly matches one key, use it even if it's also a prefix of others."""
    store = _store(
        {
            "Black": {"bpm": 90, "sig": 4},
            "Black Dog": {"bpm": 82, "sig": 4},
            "Black Star": {"bpm": 95, "sig": 4},
        }
    )
    result = store.resolve("Black")
    assert result == "Black"


def test_substring_match_when_no_prefix():
    """A word that appears in the middle of a song name resolves if it's unique."""
    result = STORE.resolve("Teen")
    assert result == "Smells Like Teen Spirit"


def test_empty_store_returns_none():
    result = _store({}).resolve("Anything")
    assert result is None


# ─── load_setlist ─────────────────────────────────────────────────────────────


def test_missing_file_returns_empty(tmp_path):
    resolved, warnings = _load(tmp_path / "nonexistent.txt")
    assert resolved == []
    assert warnings == []


def test_basic_loading(tmp_path):
    sl = tmp_path / "setlist.txt"
    sl.write_text("Black Dog\nParanoid\n")
    resolved, warnings = _load(sl)
    assert resolved == ["Black Dog", "Paranoid"]
    assert warnings == []


def test_blank_lines_and_comments_ignored(tmp_path):
    sl = tmp_path / "setlist.txt"
    sl.write_text("# Set 1\n\nParanoid\n\n# Set 2\nBlack Dog\n")
    resolved, warnings = _load(sl)
    assert resolved == ["Paranoid", "Black Dog"]
    assert warnings == []


def test_unresolvable_line_produces_warning(tmp_path):
    sl = tmp_path / "setlist.txt"
    sl.write_text("Bohemian Rhapsody\nParanoid\n")
    resolved, warnings = _load(sl)
    assert resolved == ["Paranoid"]
    assert len(warnings) == 1
    assert "Bohemian Rhapsody" in warnings[0]
    assert "no match" in warnings[0]


def test_ambiguous_line_produces_warning(tmp_path):
    sl = tmp_path / "setlist.txt"
    sl.write_text("Highway\nBlack Dog\n")
    resolved, warnings = _load(sl)
    assert resolved == ["Black Dog"]
    assert len(warnings) == 1
    assert "Highway" in warnings[0]
    assert "ambiguous" in warnings[0]


def test_warning_includes_line_number(tmp_path):
    sl = tmp_path / "setlist.txt"
    sl.write_text("Paranoid\nBohemian Rhapsody\nBlack Dog\n")
    _, warnings = _load(sl)
    assert "line 2" in warnings[0]


def test_case_insensitive_loading(tmp_path):
    sl = tmp_path / "setlist.txt"
    sl.write_text("paranoid\nBLACK DOG\n")
    resolved, warnings = _load(sl)
    assert resolved == ["Paranoid", "Black Dog"]
    assert warnings == []


def test_unique_prefix_loading(tmp_path):
    sl = tmp_path / "setlist.txt"
    sl.write_text("Smells\nParanoid\n")
    resolved, warnings = _load(sl)
    assert resolved == ["Smells Like Teen Spirit", "Paranoid"]
    assert warnings == []


def test_empty_file_returns_empty(tmp_path):
    sl = tmp_path / "setlist.txt"
    sl.write_text("")
    resolved, warnings = _load(sl)
    assert resolved == []
    assert warnings == []


# ─── editing setlist.txt ──────────────────────────────────────────────────────


def test_add_appends_once(tmp_path):
    sl = tmp_path / "setlist.txt"
    add_to_setlist(sl, "Paranoid")
    add_to_setlist(sl, "paranoid")
    assert sl.read_text() == "Paranoid\n"


def test_remove_drops_every_matching_line(tmp_path):
    sl = tmp_path / "setlist.txt"
    sl.write_text("# Set\nParanoid\nBlack Dog\nPARANOID\n")
    remove_from_setlist(sl, "paranoid")
    assert sl.read_text() == "# Set\nBlack Dog\n"


def test_remove_from_missing_file_is_a_no_op(tmp_path):
    remove_from_setlist(tmp_path / "setlist.txt", "Paranoid")
    assert not (tmp_path / "setlist.txt").exists()

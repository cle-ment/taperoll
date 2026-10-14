"""Curses front end: renders a Session and turns key presses into its actions.

Rendering is split in two: ``Tui.render()`` builds the screen as plain rows of
(text, style) segments, which ``paint()`` then writes with curses. Only the
latter touches the terminal, so layout and key handling are unit-testable.
"""

from __future__ import annotations

import curses
import enum
import time
from dataclasses import dataclass, field

import numpy as np

from .audio import LevelMeter
from .session import Session

BAR_WIDTH = 36  # level meter bar width
FRAME_SECONDS = 0.05  # ~20 fps
FOOTER = (
    "  [r] rec    [n] name    [m] metro    [b] bpm    [t] sig    [s] sub    [x] stems"
    "    [↑↓] setlist    [a] add    [d] del    [q] quit  "
)
BACKSPACE = ("KEY_BACKSPACE", "\x7f", "\b")
ENTER = ("\n", "\r")
ESCAPE = "\x1b"


class Style(enum.Enum):
    NORMAL = enum.auto()
    DIM = enum.auto()
    INFO = enum.auto()  # highlighted, e.g. an active mode
    HEADER = enum.auto()
    RECORDING = enum.auto()
    LEVEL_OK = enum.auto()
    LEVEL_HOT = enum.auto()
    LEVEL_CLIP = enum.auto()
    SELECTED = enum.auto()


Segment = tuple[str, Style]
Row = list[Segment]


# ── input prompts ─────────────────────────────────────────────────────────────


@dataclass(slots=True)
class NamePrompt:
    """Song name entry with suggestions; selected == -1 means the text field."""

    text: str
    suggestions: list[str] = field(default_factory=list)
    selected: int = -1

    @property
    def choice(self) -> str:
        return self.suggestions[self.selected] if self.selected >= 0 else self.text


@dataclass(slots=True)
class BpmPrompt:
    text: str


# ── rendering helpers ─────────────────────────────────────────────────────────


def linear_to_db(v: float) -> float:
    return 20 * float(np.log10(max(v, 1e-9)))


def _db_to_filled(db: float) -> int:
    """Map a dB value (–60…0) to a bar fill count (0…BAR_WIDTH)."""
    return int(max(0.0, min(1.0, (db + 60) / 60)) * BAR_WIDTH)


def meter_row(label: str, meter: LevelMeter) -> Row:
    """Current level (coloured) | gap up to the peak hold (▒) | empty (░)."""
    db = linear_to_db(meter.level)
    filled = _db_to_filled(db)
    filled_hold = max(filled, _db_to_filled(linear_to_db(meter.hold)))
    style = (
        Style.LEVEL_CLIP if db > -6 else Style.LEVEL_HOT if db > -18 else Style.LEVEL_OK
    )
    return [
        (f"{label} ", Style.NORMAL),
        ("█" * filled, style),
        ("▒" * (filled_hold - filled), Style.DIM),
        ("░" * (BAR_WIDTH - filled_hold), Style.DIM),
        (f" {db:+5.1f} dB", Style.NORMAL),
    ]


def _line(text: str, style: Style = Style.NORMAL) -> Row:
    return [(text, style)]


# ── the UI ────────────────────────────────────────────────────────────────────


class Tui:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.prompt: NamePrompt | BpmPrompt | None = None

    def render(self) -> list[Row]:
        s = self.session
        metro = s.metronome
        rows: list[Row] = [_line("📼 TapeRoll Rehearsal Recorder", Style.HEADER), []]
        rows.append(_line(f"Input device:  {s.audio.device_name or 'system default'}"))
        if metro.active:
            rows.append(_line(f"Output device: {metro.out_device_name}"))
        else:
            rows.append(_line("Output device: (metronome off)", Style.DIM))
        rows.append([])

        labels = ["L", "R"] if len(s.audio.meters) >= 2 else ["M"]
        rows += [
            meter_row(label, m)
            for label, m in zip(labels, s.audio.meters, strict=False)
        ]
        rows.append([])

        if isinstance(self.prompt, NamePrompt):
            rows += self._name_prompt_rows(self.prompt)
        else:
            if s.song_name:
                rows.append(_line(f"Song name: {s.song_name}"))
            else:
                rows.append(_line("Song name: (none — press n to set)", Style.DIM))
            if s.setlist:
                n = len(s.setlist)
                where = (
                    f"[{s.setlist_idx + 1}/{n}]  ↑↓ to navigate"
                    if s.setlist_idx >= 0
                    else f"{n} songs loaded  ↓ to start"
                )
                rows.append(_line(f"Setlist:   {where}", Style.DIM))

        state = "♩ ON " if metro.active else "○ OFF"
        bpm = (
            f"BPM: {self.prompt.text}_"
            if isinstance(self.prompt, BpmPrompt)
            else f"{metro.bpm} BPM"
        )
        sub = "  sub" if metro.subdivision else ""
        highlight = metro.active or isinstance(self.prompt, BpmPrompt)
        rows.append(
            _line(
                f"Metronome: {state}  {bpm}  {metro.beats_per_bar}/4{sub}",
                Style.INFO if highlight else Style.DIM,
            )
        )
        if s.audio.stems:
            rows.append(self._stems_row())
        rows.append([])

        if s.recording:
            elapsed = time.strftime("%M:%S", time.gmtime(s.elapsed))
            ts = s.take.ts if s.take else ""
            rows.append(_line(f"Recording: ● {elapsed}  {ts}", Style.RECORDING))
        else:
            rows.append(_line("Recording: ■ stopped"))
        if s.pending_rating is not None:
            rows.append(
                _line(
                    f'Rate take "{s.pending_rating.name}":  [1]–[5]   [0/Esc] skip',
                    Style.INFO,
                )
            )
        rows.append([])
        rows += [_line(text, Style.DIM) for text in s.activity.lines()]
        return rows

    def _name_prompt_rows(self, prompt: NamePrompt) -> list[Row]:
        rows = [_line(f"Song name: {prompt.text}_", Style.INFO)]
        for i, name in enumerate(prompt.suggestions):
            settings = self.session.songs.get(name)
            hint = f"  ({settings.bpm} BPM  {settings.sig}/4)" if settings else ""
            selected = i == prompt.selected
            marker = "›" if selected else " "
            rows.append(
                _line(
                    f"  {marker} {name}{hint}",
                    Style.SELECTED if selected else Style.DIM,
                )
            )
        return rows

    def _stems_row(self) -> Row:
        s = self.session
        if not s.stems_mode:
            return _line("Stems:     ○ OFF", Style.DIM)
        if s.audio.stems_unavailable:
            return _line(
                f"Stems:     ● ON — unavailable: {s.audio.stems_unavailable}",
                Style.INFO,
            )
        min_rating = s.config.stems.upload_min_rating
        upload = (
            "upload all" if min_rating is None else f"upload if rated ≥ {min_rating}"
        )
        return _line(
            f"Stems:     ● ON  {len(s.audio.stems)} ch, {upload}",
            Style.INFO,
        )

    # ── keys ──────────────────────────────────────────────────────────────────

    def handle_key(self, key: str) -> bool:
        """Handle one key press. Returns False when the app should quit."""
        if isinstance(self.prompt, NamePrompt):
            self._name_key(self.prompt, key)
        elif isinstance(self.prompt, BpmPrompt):
            self._bpm_key(self.prompt, key)
        else:
            return self._command_key(key)
        return True

    def _command_key(self, key: str) -> bool:
        s = self.session
        if key == "q":
            return not s.can_quit
        if len(key) == 1 and key in "12345" and s.pending_rating is not None:
            s.rate(int(key))
        elif key in ("0", ESCAPE) and s.pending_rating is not None:
            s.rate(None)
        elif key == "n" and not s.recording:
            self.prompt = NamePrompt(s.song_name, s.songs.suggestions(s.song_name))
        elif key == "b":
            self.prompt = BpmPrompt(str(s.metronome.bpm))
        elif action := {
            "r": s.toggle_recording,
            "KEY_DOWN": lambda: s.setlist_step(+1),
            "KEY_UP": lambda: s.setlist_step(-1),
            "a": s.setlist_add,
            "d": s.setlist_remove,
            "m": s.toggle_metronome,
            "t": s.cycle_time_signature,
            "s": s.toggle_subdivision,
            "x": s.toggle_stems,
        }.get(key):
            action()
        return True

    def _name_key(self, prompt: NamePrompt, key: str) -> None:
        if key in ENTER:
            self.prompt = None
            self.session.select_song(prompt.choice)
        elif key == ESCAPE:
            self.prompt = None
        elif key == "KEY_DOWN":
            prompt.selected = min(prompt.selected + 1, len(prompt.suggestions) - 1)
        elif key == "KEY_UP":
            prompt.selected = max(prompt.selected - 1, -1)
        elif prompt.selected == -1 and (
            key in BACKSPACE or (len(key) == 1 and key.isprintable())
        ):
            prompt.text = prompt.text[:-1] if key in BACKSPACE else prompt.text + key
            prompt.suggestions = self.session.songs.suggestions(prompt.text)

    def _bpm_key(self, prompt: BpmPrompt, key: str) -> None:
        if key in ENTER:
            self.prompt = None
            if prompt.text:
                self.session.set_bpm(int(prompt.text))
        elif key == ESCAPE:
            self.prompt = None
        elif key in BACKSPACE:
            prompt.text = prompt.text[:-1]
        elif len(key) == 1 and key.isdigit():
            prompt.text += key


# ── curses ────────────────────────────────────────────────────────────────────

_COLOURS = {
    Style.NORMAL: (curses.COLOR_WHITE, 0),
    Style.DIM: (curses.COLOR_WHITE, curses.A_DIM),
    Style.INFO: (curses.COLOR_CYAN, 0),
    Style.HEADER: (curses.COLOR_CYAN, curses.A_BOLD),
    Style.RECORDING: (curses.COLOR_RED, curses.A_BOLD),
    Style.LEVEL_OK: (curses.COLOR_GREEN, 0),
    Style.LEVEL_HOT: (curses.COLOR_YELLOW, 0),
    Style.LEVEL_CLIP: (curses.COLOR_RED, 0),
    Style.SELECTED: (curses.COLOR_CYAN, curses.A_REVERSE),
}


def _init_styles() -> dict[Style, int]:
    curses.start_color()
    curses.use_default_colors()
    attrs = {}
    for pair, (style, (colour, extra)) in enumerate(_COLOURS.items(), start=1):
        curses.init_pair(pair, colour, -1)
        attrs[style] = curses.color_pair(pair) | extra
    return attrs


def paint(stdscr: curses.window, rows: list[Row], attrs: dict[Style, int]) -> None:
    stdscr.erase()
    height, width = stdscr.getmaxyx()
    for y, row in enumerate(rows[: max(0, height - 2)]):
        x = 2
        for text, style in row:
            if x < width - 1 and text:
                stdscr.addstr(y, x, text[: width - 1 - x], attrs[style])
            x += len(text)
    if height > 2:
        stdscr.addstr(height - 1, 0, FOOTER.ljust(width)[: width - 1], curses.A_REVERSE)
    stdscr.refresh()


def run(stdscr: curses.window, session: Session) -> None:
    """The main loop: draw, handle a key, repeat, until the user quits."""
    curses.curs_set(0)
    stdscr.nodelay(True)
    attrs = _init_styles()
    tui = Tui(session)
    while True:
        paint(stdscr, tui.render(), attrs)
        try:
            key = stdscr.getkey()
        except curses.error:
            key = None
        if key is not None and not tui.handle_key(key):
            return
        time.sleep(FRAME_SECONDS)

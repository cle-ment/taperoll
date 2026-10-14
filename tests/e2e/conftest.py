"""End-to-end test driver: runs the real curses app in a pseudo-terminal.

pexpect provides the pty, pyte emulates the terminal so tests can assert on
what's actually rendered on screen, a bit like Playwright for TUIs.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pexpect
import pyte
import pytest

RUN_APP = Path(__file__).with_name("run_app.py")
ROWS, COLS = 40, 180

# Key sequences as sent in keypad (application) mode, which curses
# switches the terminal into.
KEYS = {
    "up": "\x1bOA",
    "down": "\x1bOB",
    "esc": "\x1b",
    "enter": "\r",
    "backspace": "\x7f",
}


class _Screen(pyte.Screen):
    """pyte screen plus the SU/SD scroll sequences (CSI n S / CSI n T) ncurses uses."""

    def _scroll(self, count: int, *, up: bool) -> None:
        top, bottom = self.margins or (0, self.lines - 1)
        x, y = self.cursor.x, self.cursor.y
        self.cursor.y = bottom if up else top
        for _ in range(count or 1):
            self.index() if up else self.reverse_index()
        self.cursor.x, self.cursor.y = x, y

    def scroll_up(self, count: int = 1, *_: Any, **__: Any) -> None:
        self._scroll(count, up=True)

    def scroll_down(self, count: int = 1, *_: Any, **__: Any) -> None:
        self._scroll(count, up=False)


class _Stream(pyte.ByteStream):
    csi = {**pyte.ByteStream.csi, "S": "scroll_up", "T": "scroll_down"}  # noqa: RUF012


def _env(home: Path, settings: dict[str, Any]) -> dict[str, str]:
    rclone_conf = home / "rclone.conf"
    rclone_conf.touch()
    return {
        **os.environ,
        # Not xterm: pyte lacks the "rep" capability in conda-forge's xterm terminfo.
        "TERM": "screen-256color",
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
        "ESCDELAY": "25",
        "RCLONE_CONFIG": str(rclone_conf),
        "E2E_HOME": str(home),
        "E2E_SETTINGS": json.dumps(settings),
    }


class Tui:
    """A running recorder TUI."""

    def __init__(self, home: Path, settings: dict[str, Any]) -> None:
        self.home = home
        self.screen = _Screen(COLS, ROWS)
        self._stream = _Stream(self.screen)
        self.child = pexpect.spawn(
            sys.executable,
            [str(RUN_APP)],
            env=_env(home, settings),
            dimensions=(ROWS, COLS),
        )

    # ── reading the screen ─────────────────────────────────────────────────

    def _pump(self, timeout: float = 0.05) -> None:
        with contextlib.suppress(pexpect.TIMEOUT, pexpect.EOF):
            self._stream.feed(self.child.read_nonblocking(65536, timeout=timeout))

    @property
    def text(self) -> str:
        self._pump()
        return "\n".join(line.rstrip() for line in self.screen.display)

    def wait_for(self, needle: str, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if needle in self.text:
                return
        raise AssertionError(f"{needle!r} never appeared on screen:\n{self.text}")

    def wait_until_gone(self, needle: str, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if needle not in self.text:
                return
        raise AssertionError(f"{needle!r} is still on screen:\n{self.text}")

    # ── input ──────────────────────────────────────────────────────────────

    def press(self, *keys: str) -> None:
        for key in keys:
            self.child.send(KEYS.get(key, key))
            # The app polls for input at ~20 fps; give it time to handle each key.
            time.sleep(0.1)
            self._pump()

    def type(self, text: str) -> None:
        self.press(*text)

    def record(self, seconds: float = 1.5) -> None:
        """Record one take of the given length."""
        self.press("r")
        self.wait_for("Recording: ●")
        time.sleep(seconds)
        self.press("r")
        self.wait_for("Recording: ■ stopped")

    def quit(self, timeout: float = 60.0) -> int:
        """Press q and wait for the app to exit (it finishes pending takes)."""
        self.press("q")
        deadline = time.monotonic() + timeout
        while self.child.isalive() and time.monotonic() < deadline:
            self._pump(timeout=0.2)
        self.child.close()
        assert self.child.exitstatus is not None, "app did not exit"
        return self.child.exitstatus

    def kill(self) -> None:
        if self.child.isalive():
            self.child.terminate(force=True)


class App:
    """Isolated home directory for one test, plus ways to launch the app in it."""

    def __init__(self, home: Path) -> None:
        self.home = home
        self.settings: dict[str, Any] = {}
        self._tuis: list[Tui] = []

    @property
    def recordings(self) -> Path:
        return self.home / "recordings"

    def songs(self) -> dict[str, Any]:
        return json.loads((self.home / "songs.json").read_text())

    def write_songs(self, songs: dict[str, Any]) -> None:
        (self.home / "songs.json").write_text(json.dumps(songs))

    def start(self, **settings: Any) -> Tui:
        tui = Tui(self.home, {**self.settings, **settings})
        self._tuis.append(tui)
        tui.wait_for("TapeRoll Rehearsal Recorder")
        return tui

    def run_cli(self, *args: str, **settings: Any) -> str:
        """Run a non-interactive command (e.g. --upload) and return its output."""
        output, status = pexpect.run(
            f"{sys.executable} {RUN_APP} {' '.join(args)}",
            env=_env(self.home, {**self.settings, **settings}),
            withexitstatus=True,
            timeout=60,
        )
        text = output.decode()
        assert status == 0, text
        return text

    def close(self) -> None:
        for tui in self._tuis:
            tui.kill()


@pytest.fixture
def app(tmp_path: Path) -> Iterator[App]:
    a = App(tmp_path)
    yield a
    a.close()


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "e2e" in item.path.parts:
            item.add_marker(pytest.mark.e2e)

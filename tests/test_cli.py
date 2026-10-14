"""Finishing pending takes when quitting, including Ctrl+C handling."""

from __future__ import annotations

import threading

import pytest

from taperoll.cli import _finish_processing


class InterruptedThread(threading.Thread):
    """A finished thread whose first join raises KeyboardInterrupt if asked to."""

    def __init__(self, interrupt: bool) -> None:
        super().__init__(target=lambda: None)
        self.interrupt = interrupt
        self.start()

    def join(self, timeout: float | None = None) -> None:
        if self.interrupt:
            raise KeyboardInterrupt
        super().join(timeout)


@pytest.mark.parametrize(
    ("interrupt", "last_line"),
    [
        (False, "(Ctrl+C to force quit.)"),
        (True, "Forced quit — some takes may be left unencoded."),
    ],
)
def test_finish_processing(
    capsys: pytest.CaptureFixture[str], interrupt: bool, last_line: str
) -> None:
    _finish_processing([InterruptedThread(interrupt)])
    assert capsys.readouterr().out.strip().splitlines()[-1] == last_line


def test_nothing_pending_prints_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    _finish_processing([])
    assert capsys.readouterr().out == ""

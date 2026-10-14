"""The activity log shown in the TUI, written to by the UI and background threads."""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Hashable

LOG_LINES = 8


class ActivityLog:
    """The last few status lines.

    A line posted with a key replaces the earlier line with the same key, while
    that line is still visible. Background threads use this to turn "Saving…"
    into "Saved …" without clobbering lines that other takes added meanwhile.
    """

    def __init__(self, maxlen: int = LOG_LINES) -> None:
        self._entries: deque[tuple[Hashable | None, str]] = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def add(self, text: str, key: Hashable | None = None) -> None:
        with self._lock:
            if key is not None:
                for i, (k, _) in enumerate(self._entries):
                    if k == key:
                        self._entries[i] = (key, text)
                        return
            self._entries.append((key, text))

    def lines(self) -> list[str]:
        with self._lock:
            return [text for _, text in self._entries]

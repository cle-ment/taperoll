"""Stand-in for the `sounddevice` module, for unit and end-to-end tests.

Provides two devices: a 1-input "laptop mic" (the system default) and an
8-input / 2-output "Fake DL32S". Input streams deliver a sine tone on every
channel, except the channels listed in SILENT_CHANNELS (1-based), in real time
from a background thread, just like PortAudio would.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

import numpy as np

DEVICES: list[dict[str, Any]] = [
    {"name": "Laptop Mic", "max_input_channels": 1, "max_output_channels": 2},
    {"name": "Fake DL32S", "max_input_channels": 8, "max_output_channels": 2},
]
SILENT_CHANNELS: set[int] = set()


class _Default:
    device: tuple[int, int] = (0, 0)


default = _Default()


def query_devices(device: int | str | None = None) -> Any:
    if device is None:
        return DEVICES
    if isinstance(device, int):
        return DEVICES[device]
    for d in DEVICES:
        if device.lower() in d["name"].lower():
            return d
    raise ValueError(f"No device matching {device!r}")


class _Stream:
    def __init__(
        self,
        *,
        samplerate: int,
        channels: int,
        dtype: str,
        device: int | str | None,
        blocksize: int,
        callback: Callable[..., None],
    ) -> None:
        self.samplerate = samplerate
        self.channels = channels
        self.dtype = np.dtype(dtype)
        self.blocksize = blocksize
        self.callback = callback
        self._running = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._running.set()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running.clear()
        if self._thread is not None:
            self._thread.join()

    def close(self) -> None:
        self.stop()

    def _run(self) -> None:
        period = self.blocksize / self.samplerate
        pos = 0
        while self._running.is_set():
            self._tick(pos)
            pos += self.blocksize
            time.sleep(period)

    def _tick(self, pos: int) -> None:
        raise NotImplementedError


class InputStream(_Stream):
    def _tick(self, pos: int) -> None:
        t = (pos + np.arange(self.blocksize)) / self.samplerate
        full_scale = np.iinfo(str(self.dtype)).max if self.dtype.kind == "i" else 1.0
        block = np.zeros((self.blocksize, self.channels), dtype=self.dtype)
        for ch in range(self.channels):
            if ch + 1 in SILENT_CHANNELS:
                continue
            tone = 0.25 * np.sin(2 * np.pi * (220 + 55 * ch) * t)
            block[:, ch] = (tone * full_scale).astype(self.dtype)
        self.callback(block, self.blocksize, None, None)


class OutputStream(_Stream):
    def _tick(self, pos: int) -> None:
        outdata = np.zeros((self.blocksize, self.channels), dtype=self.dtype)
        self.callback(outdata, self.blocksize, None, None)

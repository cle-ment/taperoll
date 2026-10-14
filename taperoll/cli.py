"""Command-line entry point."""

from __future__ import annotations

import argparse
import curses
import locale
import logging
import threading

import sounddevice as sd

from . import tui
from .activity import ActivityLog
from .audio import AudioInput, input_devices
from .config import Config, load_config
from .metronome import Metronome
from .session import Session
from .songs import SongStore
from .takes import TakeProcessor
from .upload import Uploader, upload_pending


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="taperoll", description="TapeRoll Rehearsal Recorder"
    )
    parser.add_argument(
        "--list-devices", action="store_true", help="list audio inputs and exit"
    )
    parser.add_argument(
        "--upload",
        action="store_true",
        help="upload local recordings missing from the destination, then exit",
    )
    args = parser.parse_args(argv)

    config, warnings = load_config()
    logging.basicConfig(
        filename=config.log_path,
        level=logging.DEBUG,
        format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    for warning in warnings:
        logging.getLogger(__name__).warning(warning)

    if args.list_devices:
        list_devices()
    elif args.upload:
        for warning in warnings:
            print(f"⚠ {warning}")
        upload_pending(config, Uploader(config))
    else:
        record(config, warnings)


def list_devices() -> None:
    default = sd.default.device[0]
    print("\nAvailable audio input devices:\n")
    for i, name, channels in input_devices():
        tag = " ← default" if i == default else ""
        print(f"  [{i:2d}]  {name}  ({channels} ch){tag}")
    print()


def record(config: Config, warnings: list[str]) -> None:
    """Run the recorder TUI, then finish pending encodes."""
    locale.setlocale(locale.LC_ALL, "")
    activity = ActivityLog()
    songs, songs_warning = SongStore.load(config.songs_path)
    session = Session(
        config,
        AudioInput(config.audio, config.channels),
        Metronome(config.metronome, config.audio.sample_rate),
        songs,
        TakeProcessor(config, activity),
        activity,
    )
    session.start([*warnings, *([songs_warning] if songs_warning else [])])
    try:
        curses.wrapper(tui.run, session)
    finally:
        pending = session.shutdown()
    _finish_processing(pending)
    print("\nGoodbye 👋\n")


def _finish_processing(pending: list[threading.Thread]) -> None:
    if not pending:
        return
    print(f"\nFinishing {len(pending)} take(s)…")
    print("(Ctrl+C to force quit.)")
    try:
        for t in pending:
            t.join()
    except KeyboardInterrupt:
        print("\nForced quit — some takes may be left unencoded.")

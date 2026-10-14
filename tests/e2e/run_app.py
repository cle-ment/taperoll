"""Launch the recorder for an end-to-end test, with fake audio and an isolated home.

Environment:
  E2E_HOME      the app's home directory (config.toml, songs.json, setlist.txt,
                recordings/) and the upload "remote", a plain local folder that
                rclone copies to for real
  E2E_SETTINGS  JSON object of overrides, see write_config()

Any command-line arguments are passed on to the app (e.g. --upload).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))  # the project, for taperoll
sys.path.insert(0, str(HERE.parent))  # tests/, for fake_sounddevice

import fake_sounddevice  # noqa: E402

sys.modules["sounddevice"] = fake_sounddevice  # type: ignore[assignment]

from taperoll.cli import main  # noqa: E402
from taperoll.config import HOME_ENV  # noqa: E402


def write_config(home: Path, settings: dict[str, Any]) -> None:
    """config.toml for the test; settings["config_toml"] is appended verbatim."""
    (home / "config.toml").write_text(
        f"""
[audio]
device = "{settings.get("audio_device", "Fake DL32S")}"

[recording]
min_seconds = {settings.get("min_recording_seconds", 1)}

[upload]
destination = "{home / "remote"}/"
"""
        + settings.get("config_toml", "")
    )


if __name__ == "__main__":
    home = Path(os.environ["E2E_HOME"])
    settings = json.loads(os.environ.get("E2E_SETTINGS", "{}"))
    fake_sounddevice.SILENT_CHANNELS = set(settings.get("silent_channels", []))
    write_config(home, settings)
    os.environ[HOME_ENV] = str(home)
    main(sys.argv[1:])

"""Uploading local recordings via rclone (`--upload`)."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .naming import STEMS_SUFFIX, recording_date, stems_rating


@dataclass(frozen=True, slots=True)
class UploadResult:
    ok: bool
    message: str


class Uploader:
    """Uploads recordings and remembers which remote folders received files."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.uploaded_dirs: set[str] = set()  # remote folders that received files

    def dest_dir(self, date: str) -> str:
        return f"{self.config.upload.destination.rstrip('/')}/{date}"

    def upload_file(self, path: Path, dest_dir: str) -> UploadResult:
        proc = _rclone("copy", str(path), dest_dir)
        if proc.returncode != 0:
            return UploadResult(False, "Upload FAILED — file kept locally")
        self._mark_uploaded(dest_dir)
        if self.config.upload.delete_after_upload:
            path.unlink()
        if cmd := self.config.upload.notify_command:
            # Split into a list (no shell); {file} is replaced in each token.
            subprocess.run(
                [tok.replace("{file}", str(path)) for tok in cmd.split()], check=False
            )
        return UploadResult(True, f"Uploaded → {dest_dir}/{path.name}")

    def upload_stems(self, folder: Path, dest_dir: str) -> UploadResult:
        """Upload a stems folder into dest_dir/<folder name>/, one file at a time."""
        for path in sorted(folder.iterdir()):
            if _rclone("copy", str(path), f"{dest_dir}/{folder.name}").returncode != 0:
                return UploadResult(False, "Upload FAILED — stems kept locally")
        self._mark_uploaded(dest_dir)
        if self.config.stems.delete_after_upload:
            shutil.rmtree(folder)
        return UploadResult(True, f"Uploaded stems → {dest_dir}/{folder.name}")

    def share_links(self) -> list[tuple[str, str]]:
        """(date, shareable URL) for every remote folder that received uploads."""
        links = []
        for dest in sorted(self.uploaded_dirs):
            proc = _rclone("link", dest)
            if proc.returncode == 0:
                links.append((dest.rsplit("/", 1)[-1], proc.stdout.strip()))
        return links

    def unavailable(self) -> str | None:
        """Why uploading can't work at all with this config, if it can't."""
        upload = self.config.upload
        if not upload.enabled:
            return "Upload disabled"
        if not upload.destination:
            return "Upload skipped — no upload.destination configured"
        if not shutil.which("rclone"):
            return "Upload skipped — rclone not found"
        return None

    def _mark_uploaded(self, dest_dir: str) -> None:
        self.uploaded_dirs.add(dest_dir)


def _rclone(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["rclone", *args], capture_output=True, text=True, check=False
    )


def upload_pending(
    config: Config, uploader: Uploader, echo: Callable[[str], None] = print
) -> None:
    """Upload every local recording that hasn't reached the destination yet.

    rclone copy is idempotent: files already at the destination are skipped,
    so this is safe to run at any time.
    """
    if reason := uploader.unavailable():
        echo(reason)
        return
    out = config.output_dir
    ext = ".mp3" if config.recording.encode_mp3 else ".wav"
    files = sorted(out.glob(f"*{ext}"))
    # Only finished stems folders (in-progress/crashed takes end in .partial)
    # of takes rated high enough; the rest stay local.
    stems = sorted(d for d in out.glob(f"*{STEMS_SUFFIX}") if d.is_dir())
    stems_kept = [d for d in stems if not config.stems.should_upload(stems_rating(d))]
    candidates = files + [d for d in stems if d not in stems_kept]
    if not candidates:
        echo(f"No {ext} files or rated stems found in {out}")
        return

    echo(
        f"Found {len(candidates)} file(s)/stems folder(s) — uploading via rclone "
        "(already-uploaded files are skipped):\n"
    )
    ok = failed = 0
    shown: set[str] = set()

    def show_links() -> None:
        for date, link in uploader.share_links():
            if date not in shown:
                shown.add(date)
                echo(f"\nRecordings {date}: {link}")

    # Mixdowns first, so they can be shared while the (large) stems still upload.
    if files:
        echo("Uploading mixdowns…")
    stems_started = False
    for path in candidates:
        if path.is_dir() and not stems_started:
            stems_started = True
            show_links()
            echo("\nUploading stems…")
        dest_dir = uploader.dest_dir(recording_date(path))
        echo(f"  {path.name}  →  {dest_dir}  …")
        upload = uploader.upload_stems if path.is_dir() else uploader.upload_file
        result = upload(path, dest_dir)
        echo(f"    {'Uploaded ✓' if result.ok else result.message}")
        ok += result.ok
        failed += not result.ok
    echo(f"\nDone: {ok} uploaded, {failed} failed.")
    if stems_kept:
        echo(
            f"{len(stems_kept)} stems folder(s) kept locally "
            f"(rated below {config.stems.upload_min_rating} or unrated)."
        )
    show_links()

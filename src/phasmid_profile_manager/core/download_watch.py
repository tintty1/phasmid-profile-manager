"""Downloads an automation client starts, kept on the server under their own names.

A CDP client decides where the browser downloads: Playwright's
``connect_over_cdp`` sends ``Browser.setDownloadBehavior`` with a folder on the
*client's* machine, which does not exist when the browser runs in Docker or on
another host. And with ``behavior: allow`` Chrome overwrites a file of the same
name, so a second ``export.csv`` destroys the first.

So the CDP relay (``api/routes/automation.py``) rewrites that call: downloads
go to ``<profile downloads>/.incoming/`` named by their GUID
(``allowAndName``), with events on. It watches the events the browser sends
back: ``Browser.downloadWillBegin`` names the GUID, and on
``Browser.downloadProgress`` ``completed`` the file is moved into the profile's
downloads folder under its own name, made unique the way Chrome does it
(``name (1).ext``). The registry keeps GUID -> name, so a client that knows its
download's GUID (Playwright: the file name of ``download.path()``) can find the
exact file even when names repeat.

The registry lives in memory: it describes downloads this process watched. A
download that finishes while no relay is attached stays in ``.incoming`` and is
moved by :meth:`DownloadRegistry.sweep` the next time anyone lists the folder.
"""

from __future__ import annotations

import json
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

INCOMING = ".incoming"
# An unknown file in .incoming younger than this may still be being written.
SETTLE_SECONDS = 30.0
_BEHAVIOURS_TO_KEEP = ("allow", "allowAndName")


def incoming_dir(folder: Path) -> Path:
    return folder / INCOMING


def safe_name(name: str | None) -> str:
    """A bare, visible file name for whatever the site suggested."""
    base = (name or "").replace("\\", "/").rsplit("/", 1)[-1].strip().lstrip(".")
    base = base.replace("\0", "")
    return base or "download"


def unique_path(folder: Path, name: str) -> Path:
    """``folder/name``, or ``name (1).ext``, ``name (2).ext``... if taken."""
    candidate = folder / name
    if not candidate.exists():
        return candidate
    stem, dot, suffix = name.partition(".") if not name.startswith(".") else (name, "", "")
    # Keep compound suffixes together: "data.tar.gz" -> "data (1).tar.gz".
    for index in range(1, 10_000):
        candidate = folder / f"{stem} ({index}){dot}{suffix}"
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"Too many downloads named {name!r}")


def rewrite_client_message(text: str, folder: Path) -> str | None:
    """The client's message with download behaviour pointed at the server, or None.

    Only ``Browser.setDownloadBehavior`` asking to allow downloads is touched;
    ``deny`` and ``default`` pass through as sent.
    """
    if "Browser.setDownloadBehavior" not in text:
        return None
    try:
        message = json.loads(text)
    except ValueError:
        return None
    if not isinstance(message, dict) or message.get("method") != "Browser.setDownloadBehavior":
        return None
    params = message.get("params")
    if not isinstance(params, dict) or params.get("behavior") not in _BEHAVIOURS_TO_KEEP:
        return None
    incoming = incoming_dir(folder)
    incoming.mkdir(parents=True, exist_ok=True)
    params["behavior"] = "allowAndName"
    params["downloadPath"] = str(incoming)
    params["eventsEnabled"] = True
    return json.dumps(message)


def download_event(text: str) -> tuple[str, dict[str, Any]] | None:
    """(method, params) of a Browser.download* event from the browser, or None."""
    if '"Browser.download' not in text:
        return None
    try:
        message = json.loads(text)
    except ValueError:
        return None
    method = message.get("method") if isinstance(message, dict) else None
    if method not in ("Browser.downloadWillBegin", "Browser.downloadProgress"):
        return None
    params = message.get("params")
    return (method, params) if isinstance(params, dict) else None


@dataclass
class Download:
    profile_id: str
    guid: str
    suggested: str
    url: str | None = None
    # Set when it is in the downloads folder under its own name.
    name: str | None = None
    state: str = "inProgress"


class DownloadRegistry:
    """GUID -> download, for the downloads the relays of this process watched."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._downloads: dict[str, Download] = {}

    def observe(self, profile_id: str, folder: Path, method: str, params: dict[str, Any]) -> None:
        guid = params.get("guid")
        if not isinstance(guid, str) or not guid or "/" in guid or guid.startswith("."):
            return
        if method == "Browser.downloadWillBegin":
            with self._lock:
                self._downloads.setdefault(
                    guid,
                    Download(
                        profile_id,
                        guid,
                        safe_name(params.get("suggestedFilename")),
                        params.get("url"),
                    ),
                )
            return
        state = params.get("state")
        if state == "completed":
            self._finish(profile_id, folder, guid)
        elif state == "canceled":
            with self._lock:
                download = self._downloads.get(guid)
                if download is not None:
                    download.state = "canceled"
            (incoming_dir(folder) / guid).unlink(missing_ok=True)

    def _finish(self, profile_id: str, folder: Path, guid: str) -> None:
        with self._lock:
            download = self._downloads.get(guid)
            if download is None:
                download = Download(profile_id, guid, guid)
                self._downloads[guid] = download
            if download.name is not None:
                return  # two relays on one browser both saw it
            source = incoming_dir(folder) / guid
            if not source.is_file():
                return
            target = unique_path(folder, download.suggested)
            shutil.move(str(source), str(target))
            download.name = target.name
            download.state = "completed"

    def sweep(self, profile_id: str, folder: Path) -> None:
        """Move finished downloads nobody watched out of ``.incoming``.

        A file there whose download this process knows and saw complete is
        moved by :meth:`observe`; one it does not know about (the relay was
        gone when it finished, or the app restarted) is moved under its GUID,
        so it is at least visible and fetchable.
        """
        incoming = incoming_dir(folder)
        if not incoming.is_dir():
            return
        for path in incoming.iterdir():
            if not path.is_file():
                continue
            with self._lock:
                download = self._downloads.get(path.name)
            if download is not None and download.state == "inProgress":
                continue
            if download is None and time.time() - path.stat().st_mtime < SETTLE_SECONDS:
                continue
            target = unique_path(folder, download.suggested if download else path.name)
            shutil.move(str(path), str(target))
            if download is not None:
                download.name, download.state = target.name, "completed"

    def by_name(self, profile_id: str) -> dict[str, Download]:
        with self._lock:
            return {
                d.name: d
                for d in self._downloads.values()
                if d.profile_id == profile_id and d.name is not None
            }

    def in_progress(self, profile_id: str) -> list[Download]:
        with self._lock:
            return [
                d
                for d in self._downloads.values()
                if d.profile_id == profile_id and d.state == "inProgress"
            ]

    def forget(self, profile_id: str) -> None:
        with self._lock:
            for guid in [g for g, d in self._downloads.items() if d.profile_id == profile_id]:
                del self._downloads[guid]

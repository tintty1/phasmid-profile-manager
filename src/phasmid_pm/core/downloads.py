"""Each profile's downloads, in a folder of its own that the API can serve.

Chrome saves to the user's ``~/Downloads`` by default: one folder shared by
every profile, outside the Docker volume, and on a machine an agent may not be
on. So each profile gets ``<data dir>/downloads/<profile id>/``, and the browser
is pointed at it in two ways:

- the ``download.default_directory`` preference, written into the profile's
  ``Default/Preferences`` before every launch, for downloads a person starts;
- ``Browser.setDownloadBehavior`` over CDP, which an automation client sends
  after it connects (Playwright sends its own, pointing at a temporary folder
  on the *client's* machine, which is why the skill's helper sends this one
  after it). See :func:`cdp_download_behavior`.

The folder is not part of the browser data directory, so exports, backups and
clones do not carry downloaded files; permanently deleting the profile removes
it.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

# Chrome writes a download under this suffix and renames it when it completes.
PARTIAL_SUFFIX = ".crdownload"


def downloads_dir(data_dir: Path, profile_id: str) -> Path:
    return data_dir / "downloads" / profile_id


def point_preferences_at(user_data_dir: Path, folder: Path) -> None:
    """Make the profile's own download directory the browser's default.

    Merged into ``Default/Preferences`` so every other preference survives. A
    file Chrome cannot parse is left alone: Chrome would reset it anyway, and
    overwriting it here would destroy whatever it still holds.
    """
    folder.mkdir(parents=True, exist_ok=True)
    path = user_data_dir / "Default" / "Preferences"
    prefs: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(loaded, dict):
            return
        prefs = loaded
    download = prefs.setdefault("download", {})
    download["default_directory"] = str(folder)
    download["prompt_for_download"] = False
    download["directory_upgrade"] = True
    prefs.setdefault("savefile", {})["default_directory"] = str(folder)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name("Preferences.ppm-tmp")
    temp.write_text(json.dumps(prefs), encoding="utf-8")
    os.replace(temp, path)


def cdp_download_behavior(folder: Path) -> dict[str, Any]:
    """The ``Browser.setDownloadBehavior`` params that send downloads to ``folder``.

    ``allow`` keeps the site's file name (Chrome adds `` (1)`` on a clash);
    ``allowAndName`` would name files by GUID, which nobody can find again.
    """
    return {"behavior": "allow", "downloadPath": str(folder), "eventsEnabled": True}


def list_files(folder: Path) -> list[dict[str, Any]]:
    """The files in a profile's downloads folder, newest first."""
    if not folder.is_dir():
        return []
    entries: list[dict[str, Any]] = []
    for path in folder.iterdir():
        if not path.is_file() or path.is_symlink() or path.name.startswith("."):
            continue
        stat = path.stat()
        partial = path.name.endswith(PARTIAL_SUFFIX)
        entries.append(
            {
                "name": path.name,
                "size": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime),
                "complete": not partial,
            }
        )
    entries.sort(key=lambda e: e["modified"].timestamp(), reverse=True)
    return entries


def resolve_file(folder: Path, name: str) -> Path:
    """One file in the folder, by its bare name. Raises FileNotFoundError.

    A name with a separator, ``..`` or a leading dot is refused outright, and
    the resolved path must still sit directly in the folder, so a request can
    never reach outside it (or follow a symlink out of it).
    """
    if not name or "/" in name or "\\" in name or name.startswith(".") or "\0" in name:
        raise FileNotFoundError(name)
    path = folder / name
    if path.is_symlink() or not path.is_file() or path.resolve().parent != folder.resolve():
        raise FileNotFoundError(name)
    return path

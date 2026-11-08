"""Naming and filing the downloads CDP clients start (core/download_watch.py)."""

import os
import time

from phasmid_profile_manager.core import download_watch
from phasmid_profile_manager.core.download_watch import DownloadRegistry, safe_name, unique_path


def test_a_taken_name_gets_a_number_like_chrome_gives_it(tmp_path):
    assert unique_path(tmp_path, "a.csv").name == "a.csv"
    (tmp_path / "a.csv").write_text("")
    assert unique_path(tmp_path, "a.csv").name == "a (1).csv"
    (tmp_path / "a (1).csv").write_text("")
    assert unique_path(tmp_path, "a.csv").name == "a (2).csv"
    (tmp_path / "data.tar.gz").write_text("")
    assert unique_path(tmp_path, "data.tar.gz").name == "data (1).tar.gz"
    (tmp_path / "README").write_text("")
    assert unique_path(tmp_path, "README").name == "README (1)"


def test_a_suggested_name_cannot_point_anywhere_else():
    assert safe_name("../../etc/passwd") == "passwd"
    assert safe_name("C:\\Users\\x\\evil.exe") == "evil.exe"
    assert safe_name(".bashrc") == "bashrc"
    assert safe_name("") == "download"
    assert safe_name(None) == "download"


def test_only_download_events_are_parsed():
    assert download_watch.download_event('{"method": "Page.loadEventFired", "params": {}}') is None
    assert download_watch.download_event("not json, but Browser.download") is None
    event = download_watch.download_event(
        '{"method": "Browser.downloadProgress", "params": {"guid": "g", "state": "inProgress"}}'
    )
    assert event == ("Browser.downloadProgress", {"guid": "g", "state": "inProgress"})


def test_a_canceled_download_leaves_nothing(tmp_path):
    registry = DownloadRegistry()
    (tmp_path / ".incoming").mkdir()
    (tmp_path / ".incoming" / "g").write_text("part")
    registry.observe(
        "p", tmp_path, "Browser.downloadWillBegin", {"guid": "g", "suggestedFilename": "x.zip"}
    )
    assert [d.suggested for d in registry.in_progress("p")] == ["x.zip"]

    registry.observe("p", tmp_path, "Browser.downloadProgress", {"guid": "g", "state": "canceled"})

    assert not (tmp_path / ".incoming" / "g").exists()
    assert registry.in_progress("p") == []


def test_a_guid_with_a_path_in_it_is_ignored(tmp_path):
    registry = DownloadRegistry()
    registry.observe(
        "p", tmp_path, "Browser.downloadWillBegin", {"guid": "../x", "suggestedFilename": "a"}
    )
    assert registry.in_progress("p") == []


def test_a_download_nobody_watched_is_swept_out_once_settled(tmp_path):
    registry = DownloadRegistry()
    incoming = tmp_path / ".incoming"
    incoming.mkdir()
    (incoming / "fresh").write_text("maybe still writing")
    (incoming / "old").write_text("done")
    past = time.time() - 3600
    os.utime(incoming / "old", (past, past))

    registry.sweep("p", tmp_path)

    assert (tmp_path / "old").read_text() == "done"
    assert (incoming / "fresh").exists(), "a file that may still be written stays put"

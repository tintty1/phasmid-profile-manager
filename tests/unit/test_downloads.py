"""Each profile's downloads folder: the preference, the listing, safe lookups."""

import json
import os

import pytest

from phasmid_profile_manager.core import downloads


def test_the_preference_points_at_the_folder_and_keeps_the_rest(tmp_path):
    udd = tmp_path / "udd"
    (udd / "Default").mkdir(parents=True)
    (udd / "Default" / "Preferences").write_text(
        json.dumps({"profile": {"name": "Person 1"}, "download": {"extensions_to_open": ""}})
    )
    folder = tmp_path / "downloads" / "p1"

    downloads.point_preferences_at(udd, folder)

    prefs = json.loads((udd / "Default" / "Preferences").read_text())
    assert prefs["download"]["default_directory"] == str(folder)
    assert prefs["download"]["prompt_for_download"] is False
    assert prefs["download"]["extensions_to_open"] == ""
    assert prefs["savefile"]["default_directory"] == str(folder)
    assert prefs["profile"] == {"name": "Person 1"}
    assert folder.is_dir()


def test_a_first_launch_gets_a_preferences_file(tmp_path):
    downloads.point_preferences_at(tmp_path / "udd", tmp_path / "dl")
    prefs = json.loads((tmp_path / "udd" / "Default" / "Preferences").read_text())
    assert prefs["download"]["default_directory"] == str(tmp_path / "dl")


def test_an_unreadable_preferences_file_is_left_for_chrome(tmp_path):
    path = tmp_path / "udd" / "Default" / "Preferences"
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    downloads.point_preferences_at(tmp_path / "udd", tmp_path / "dl")
    assert path.read_text() == "{not json"


def test_the_listing_is_newest_first_and_marks_partial_files(tmp_path):
    (tmp_path / "old.pdf").write_bytes(b"1")
    os.utime(tmp_path / "old.pdf", (1_000_000, 1_000_000))
    (tmp_path / "new.csv").write_bytes(b"22")
    (tmp_path / "big.zip.crdownload").write_bytes(b"333")
    (tmp_path / ".hidden").write_bytes(b"x")
    (tmp_path / "subdir").mkdir()

    files = downloads.list_files(tmp_path)

    assert [f["name"] for f in files][-1] == "old.pdf"
    assert {f["name"] for f in files} == {"old.pdf", "new.csv", "big.zip.crdownload"}
    assert {f["name"]: f["complete"] for f in files}["big.zip.crdownload"] is False
    assert downloads.list_files(tmp_path / "missing") == []


@pytest.mark.parametrize("name", ["../secret", "a/b", "..", ".hidden", "", "x\\y", "nope.txt"])
def test_only_a_file_directly_in_the_folder_can_be_reached(tmp_path, name):
    folder = tmp_path / "dl"
    folder.mkdir()
    (tmp_path / "secret").write_text("no")
    (folder / ".hidden").write_text("no")
    with pytest.raises(FileNotFoundError):
        downloads.resolve_file(folder, name)


def test_a_symlink_out_of_the_folder_is_refused(tmp_path):
    folder = tmp_path / "dl"
    folder.mkdir()
    (tmp_path / "secret").write_text("no")
    (folder / "link.txt").symlink_to(tmp_path / "secret")
    with pytest.raises(FileNotFoundError):
        downloads.resolve_file(folder, "link.txt")


def test_the_cdp_behaviour_keeps_the_sites_file_name(tmp_path):
    params = downloads.cdp_download_behavior(tmp_path)
    assert params == {"behavior": "allow", "downloadPath": str(tmp_path), "eventsEnabled": True}

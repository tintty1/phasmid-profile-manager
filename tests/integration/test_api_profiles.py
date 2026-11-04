"""Integration tests for the profiles API."""

from datetime import datetime

import pytest

from phasmid_pm.api.dependencies import get_profile_manager
from phasmid_pm.core.models import UsageStats
from tests.fakes import fake_pin


@pytest.mark.asyncio
async def test_create_and_get_profile(client):
    created = await client.post("/api/profiles", json={"name": "acct-1"})
    assert created.status_code == 201
    profile_id = created.json()["id"]

    fetched = await client.get(f"/api/profiles/{profile_id}")
    assert fetched.status_code == 200
    assert fetched.json()["name"] == "acct-1"


@pytest.mark.asyncio
async def test_list_profiles_paginated(client):
    for i in range(3):
        await client.post("/api/profiles", json={"name": f"p{i}"})
    listed = await client.get("/api/profiles", params={"page": 1, "per_page": 2})
    assert listed.status_code == 200
    body = listed.json()
    assert body["total"] == 3
    assert len(body["profiles"]) == 2
    assert body["has_next"] is True


@pytest.mark.asyncio
async def test_search_profiles_by_name(client):
    await client.post("/api/profiles", json={"name": "facebook-main"})
    await client.post("/api/profiles", json={"name": "twitter-alt"})
    found = await client.get("/api/profiles", params={"search": "facebook"})
    names = [p["name"] for p in found.json()["profiles"]]
    assert names == ["facebook-main"]


@pytest.mark.asyncio
async def test_update_profile(client):
    created = await client.post("/api/profiles", json={"name": "before"})
    profile_id = created.json()["id"]
    updated = await client.put(f"/api/profiles/{profile_id}", json={"name": "after"})
    assert updated.status_code == 200
    assert updated.json()["name"] == "after"


@pytest.mark.asyncio
async def test_delete_profile(client):
    created = await client.post("/api/profiles", json={"name": "temp"})
    profile_id = created.json()["id"]
    deleted = await client.delete(f"/api/profiles/{profile_id}")
    assert deleted.status_code == 200
    missing = await client.get(f"/api/profiles/{profile_id}")
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_unknown_profile_returns_404(client):
    response = await client.get("/api/profiles/does-not-exist")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_partial_browser_settings_update_keeps_the_rest(client):
    """Editing one field must not reset the others."""
    created = await client.post(
        "/api/profiles",
        json={"name": "fp", "browser_settings": {"os": "windows", "gpu": "0x8086:0x3185"}},
    )
    profile_id = created.json()["id"]

    updated = await client.put(
        f"/api/profiles/{profile_id}",
        json={"browser_settings": {"timezone": "Asia/Tokyo"}},
    )
    assert updated.status_code == 200
    settings = updated.json()["browser_settings"]

    assert settings["timezone"] == "Asia/Tokyo"
    # Values the client never sent must survive untouched.
    assert settings["os"] == "windows"
    assert settings["gpu"] == "0x8086:0x3185"
    assert settings["webrtc_policy"] == "default"


@pytest.mark.asyncio
async def test_create_generates_a_machine_for_the_settings(client, phasmid):
    """With the browser installed, a new profile is a machine straight away.

    Geography is left unset so it follows the proxy: the machine is generated
    at this computer's zone and moved to the exit on its first launch.
    """
    created = await client.post(
        "/api/profiles",
        json={"name": "generated", "browser_settings": {"os": "windows"}},
    )
    assert created.status_code == 201
    body = created.json()

    assert body["browser_settings"]["os"] == "windows"
    assert body["browser_settings"]["timezone"] is None
    assert body["browser_settings"]["languages"] is None
    assert body["fingerprint"]["os"] == "windows"
    assert body["fingerprint"]["hardware_concurrency"]
    assert body["fingerprint"]["public_ip"] is None, "no exit is looked up at creation"
    (request,) = phasmid.genprofile.generated()
    assert request["os"] == "windows" and "exit_ip" not in request


@pytest.mark.asyncio
async def test_without_the_browser_the_first_launch_generates_the_machine(client, phasmid):
    phasmid.installed = False
    created = await client.post("/api/profiles", json={"name": "unlaunched"})
    assert created.status_code == 201
    assert created.json()["fingerprint"] is None
    assert phasmid.genprofile.generated() == []


@pytest.mark.asyncio
async def test_a_gpu_the_os_has_no_capture_for_is_refused(client):
    response = await client.post(
        "/api/profiles",
        json={"name": "nope", "browser_settings": {"os": "windows", "gpu": "0x10de:0x2504"}},
    )
    assert response.status_code == 400
    assert "backs gpu" in response.json()["detail"]


@pytest.mark.asyncio
async def test_a_malformed_gpu_is_refused(client):
    response = await client.post(
        "/api/profiles", json={"name": "nope", "browser_settings": {"gpu": "RTX 3060"}}
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_the_gpu_catalogue_is_listed(client):
    response = await client.get("/api/v1/fingerprints/gpus", params={"os": "linux"})
    assert response.status_code == 200
    gpus = response.json()["data"]["gpus"]
    assert {g["gpu"] for g in gpus} == {"0x10de:0x2504", "0x10de:0x2482"}
    assert all(g["os"] == "linux" and g["renderer"] for g in gpus)


@pytest.mark.asyncio
async def test_the_whole_machine_can_be_read(client):
    created = await client.post("/api/profiles", json={"name": "inspect"})
    machine = await client.get(f"/api/v1/profiles/{created.json()['id']}/machine")
    assert machine.status_code == 200
    assert machine.json()["seed"] == created.json()["fingerprint"]["seed"]
    assert machine.json()["gpu"]["device_id"]


@pytest.mark.asyncio
async def test_reading_a_machine_that_does_not_exist_yet_is_a_404(client, phasmid):
    phasmid.installed = False
    created = await client.post("/api/profiles", json={"name": "bare"})
    response = await client.get(f"/api/v1/profiles/{created.json()['id']}/machine")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_create_keeps_the_notes_it_was_given(client):
    """Notes were accepted by the schema, then overwritten with a timestamp."""
    created = await client.post("/api/profiles", json={"name": "noted", "notes": "keep me"})
    assert created.json()["notes"] == "keep me"

    blank = await client.post("/api/profiles", json={"name": "unnoted"})
    assert blank.json()["notes"] is None


@pytest.mark.asyncio
async def test_explicit_null_clears_a_value(client):
    """An explicit null is a deliberate 'none': here, follow the proxy."""
    created = await client.post(
        "/api/profiles",
        json={"name": "no-tz", "browser_settings": {"os": "windows", "timezone": None}},
    )
    assert created.json()["browser_settings"]["timezone"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,payload", [("notes", None), ("group", None), ("proxy_config", None)]
)
async def test_explicit_null_clears_the_field(client, field, payload):
    """Clearing a field must persist rather than silently report success."""
    created = await client.post(
        "/api/profiles",
        json={
            "name": "clearable",
            "notes": "keep me",
            "proxy_config": {"type": "http", "server": "1.2.3.4:8080"},
        },
    )
    profile_id = created.json()["id"]
    await client.put(f"/api/profiles/{profile_id}", json={"group": "g1"})

    updated = await client.put(f"/api/profiles/{profile_id}", json={field: payload})
    assert updated.status_code == 200
    assert updated.json()[field] is None


@pytest.mark.asyncio
async def test_omitted_fields_are_left_alone(client):
    """The flip side: not sending a field must not clear it."""
    created = await client.post(
        "/api/profiles",
        json={
            "name": "untouched",
            "notes": "keep me",
            "proxy_config": {"type": "http", "server": "1.2.3.4:8080"},
        },
    )
    profile_id = created.json()["id"]

    updated = await client.put(f"/api/profiles/{profile_id}", json={"name": "renamed"})
    assert updated.status_code == 200
    body = updated.json()
    assert body["name"] == "renamed"
    assert body["notes"] == "keep me"
    assert body["proxy_config"]["server"] == "1.2.3.4:8080"


@pytest.mark.asyncio
async def test_a_profile_round_trips_through_an_archive(client):
    """Export then import must reproduce the profile, with an id of its own."""
    created = await client.post(
        "/api/profiles",
        json={
            "name": "warmed-up",
            "notes": "months of history",
            "browser_settings": {"os": "windows", "timezone": "Europe/Berlin"},
            "proxy_config": {"type": "http", "server": "1.2.3.4:8080", "password": "secret"},
        },
    )
    original = created.json()

    exported = await client.get(f"/api/profiles/{original['id']}/export")
    assert exported.status_code == 200
    assert exported.headers["content-type"] == "application/zip"
    assert "warmed-up.phasmid.zip" in exported.headers["content-disposition"]

    imported = await client.post(
        "/api/profiles/import",
        files={"file": ("warmed-up.phasmid.zip", exported.content, "application/zip")},
    )
    assert imported.status_code == 201
    restored = imported.json()

    assert restored["name"] == "warmed-up"
    assert restored["notes"] == "months of history"
    assert restored["browser_settings"]["timezone"] == "Europe/Berlin"
    assert restored["proxy_config"]["server"] == "1.2.3.4:8080"
    assert restored["id"] != original["id"], "an import must not collide with its source"
    # The source's group id is local to that instance; it must not dangle here.
    assert restored["group"] is None


@pytest.mark.asyncio
async def test_importing_a_pinned_profile_keeps_the_machine(client):
    """The point of the archive: the same identity comes back."""
    from phasmid_pm.api.dependencies import get_profile_manager

    created = await client.post("/api/profiles", json={"name": "pinned-export"})
    manager = get_profile_manager()
    profile = await manager.get_profile(created.json()["id"])
    pin = fake_pin("windows", seed="ab" * 16, public_ip="203.0.113.10")
    profile.fingerprint = pin
    await manager.storage.update_profile(profile)

    exported = await client.get(f"/api/profiles/{profile.id}/export")
    imported = await client.post(
        "/api/profiles/import",
        files={"file": ("p.zip", exported.content, "application/zip")},
    )

    restored = await manager.get_profile(imported.json()["id"])
    assert restored.fingerprint == pin


@pytest.mark.asyncio
async def test_import_can_rename(client):
    created = await client.post("/api/profiles", json={"name": "original"})
    exported = await client.get(f"/api/profiles/{created.json()['id']}/export")

    imported = await client.post(
        "/api/profiles/import",
        params={"name": "renamed on import"},
        files={"file": ("p.zip", exported.content, "application/zip")},
    )
    assert imported.json()["name"] == "renamed on import"


@pytest.mark.asyncio
async def test_importing_rubbish_is_a_400(client):
    response = await client.post(
        "/api/profiles/import",
        files={"file": ("notes.txt", b"this is not a zip", "text/plain")},
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_exporting_an_unknown_profile_is_a_404(client):
    response = await client.get("/api/profiles/does-not-exist/export")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_a_refused_import_leaves_nothing_behind(client, monkeypatch):
    """A rejected archive must not strand a profile directory nobody can see."""
    from phasmid_pm.api.dependencies import get_profile_manager
    from phasmid_pm.core import profile_archive

    manager = get_profile_manager()
    profiles_dir = manager.profiles_dir

    # A real archive with real browser data in it, refused part-way through
    # extraction. Without data files the archive is empty and nothing is refused.
    created = await client.post("/api/profiles", json={"name": "source"})
    source = await manager.get_profile(created.json()["id"])
    source_dir = profiles_dir / f"profile_{source.id}"
    source_dir.mkdir(parents=True, exist_ok=True)
    (source_dir / "cookies.sqlite").write_bytes(b"C" * 4000)

    exported = await client.get(f"/api/profiles/{source.id}/export")
    monkeypatch.setattr(profile_archive, "_MAX_EXTRACTED_BYTES", 10)

    before = set(profiles_dir.glob("*"))
    response = await client.post(
        "/api/profiles/import", files={"file": ("p.zip", exported.content, "application/zip")}
    )
    assert response.status_code == 400

    after = set(profiles_dir.glob("*"))
    assert after == before, f"refused import left {after - before}"


WINDOWS_PIN = fake_pin("windows", seed="12" * 16, public_ip="203.0.113.10")


async def _pinned_windows_profile_set_to(client, os_name: str) -> str:
    """A profile whose pin says Windows while its setting says something else."""
    from phasmid_pm.api.dependencies import get_profile_manager

    created = await client.post("/api/profiles", json={"name": f"drifted-{os_name}"})
    profile_id = created.json()["id"]
    manager = get_profile_manager()
    profile = await manager.get_profile(profile_id)
    profile.fingerprint = dict(WINDOWS_PIN)
    profile.browser_settings.os = os_name
    await manager.storage.update_profile(profile)
    return profile_id


@pytest.mark.asyncio
async def test_a_setting_that_disagrees_with_the_pin_is_reported(client):
    """Changing the OS of a pinned profile is allowed, but must not stay silent."""
    profile_id = await _pinned_windows_profile_set_to(client, "windows")

    updated = await client.put(
        f"/api/profiles/{profile_id}", json={"browser_settings": {"os": "linux"}}
    )
    assert updated.status_code == 200
    summary = updated.json()["fingerprint"]
    assert summary["settings_os"] == "linux"
    assert summary["pinned_os"] == "windows"
    assert summary["os_mismatch"] is True


@pytest.mark.asyncio
async def test_keeping_the_machine_moves_the_setting_and_nothing_else(client):
    """The cheap way out: the profile is the computer it has always been."""
    from phasmid_pm.api.dependencies import get_profile_manager

    profile_id = await _pinned_windows_profile_set_to(client, "linux")

    response = await client.post(
        f"/api/profiles/{profile_id}/reconcile-os", json={"keep_machine": True}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["browser_settings"]["os"] == "windows"
    assert body["fingerprint"]["os_mismatch"] is False

    stored = await get_profile_manager().get_profile(profile_id)
    assert stored.fingerprint == WINDOWS_PIN, "no fingerprint may change on this path"


@pytest.mark.asyncio
async def test_keeping_the_setting_pins_a_machine_for_it(client, phasmid):
    """The expensive way out: new hardware, generated for the OS that was chosen."""
    from phasmid_pm.api.dependencies import get_profile_manager

    profile_id = await _pinned_windows_profile_set_to(client, "linux")

    response = await client.post(
        f"/api/profiles/{profile_id}/reconcile-os", json={"keep_machine": False}
    )
    assert response.status_code == 200
    body = response.json()
    assert phasmid.genprofile.generated()[-1]["os"] == "linux"
    assert body["fingerprint"]["pinned_os"] == "linux"
    assert body["fingerprint"]["os_mismatch"] is False

    stored = await get_profile_manager().get_profile(profile_id)
    assert stored.fingerprint["seed"] != WINDOWS_PIN["seed"], "this path changes the hardware"


@pytest.mark.asyncio
async def test_reconciling_a_profile_that_already_agrees_is_refused(client):
    """Otherwise a stale UI could replace the hardware of a healthy profile."""
    profile_id = await _pinned_windows_profile_set_to(client, "windows")
    response = await client.post(
        f"/api/profiles/{profile_id}/reconcile-os", json={"keep_machine": False}
    )
    assert response.status_code == 400
    assert "already set to windows" in response.json()["detail"]


@pytest.mark.asyncio
async def test_reconciling_needs_a_pin_and_a_profile(client, phasmid):
    phasmid.installed = False
    created = await client.post("/api/profiles", json={"name": "unpinned"})
    unpinned = await client.post(
        f"/api/profiles/{created.json()['id']}/reconcile-os", json={"keep_machine": True}
    )
    assert unpinned.status_code == 400
    assert "no pinned machine" in unpinned.json()["detail"]

    missing = await client.post(
        "/api/profiles/does-not-exist/reconcile-os", json={"keep_machine": True}
    )
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_clear_geography_hands_the_location_back_to_the_proxy(client, phasmid):
    """A stated zone can be dropped; the next launch then moves to the exit."""
    from phasmid_pm.api.dependencies import get_profile_manager

    old = await client.post(
        "/api/profiles",
        json={
            "name": "shanghai",
            "browser_settings": {"timezone": "Asia/Shanghai", "languages": ["zh-CN", "zh"]},
        },
    )
    profile_id = old.json()["id"]

    response = await client.post(
        "/api/profiles/clear-geography", json={"profile_ids": [profile_id]}
    )
    assert response.status_code == 200
    assert response.json() == {"cleared": [profile_id], "unchanged": [], "not_found": []}

    settings = (await client.get(f"/api/profiles/{profile_id}")).json()["browser_settings"]
    assert settings["timezone"] is None
    assert settings["languages"] == ["zh-CN", "zh"], "languages are identity, not geography"

    phasmid.exit_ip = "203.0.113.20"
    launched = await client.post(f"/api/profiles/{profile_id}/launch", json={"mode": "headless"})
    assert launched.status_code == 200
    pin = (await get_profile_manager().get_profile(profile_id)).fingerprint
    assert pin["locale"]["timezone"] == "Asia/Tokyo", "the whole point: the exit decides"
    assert pin["locale"]["languages"] == ["zh-CN", "zh"]


@pytest.mark.asyncio
async def test_clear_geography_reports_each_profile_it_was_given(client):
    """A selection is the unit here, so partial answers have to be readable."""
    with_geo = await client.post(
        "/api/profiles", json={"name": "berlin", "browser_settings": {"timezone": "Europe/Berlin"}}
    )
    without = await client.post("/api/profiles", json={"name": "follows-the-proxy"})

    response = await client.post(
        "/api/profiles/clear-geography",
        json={"profile_ids": [with_geo.json()["id"], without.json()["id"], "nope"]},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["cleared"] == [with_geo.json()["id"]]
    assert body["unchanged"] == [without.json()["id"]]
    assert body["not_found"] == ["nope"]


@pytest.mark.asyncio
async def test_clear_geography_needs_at_least_one_profile(client):
    response = await client.post("/api/profiles/clear-geography", json={"profile_ids": []})
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_regenerating_gives_a_new_machine(client):
    """Otherwise the profile would keep its old hardware forever."""
    created = await client.post("/api/profiles", json={"name": "repin"})
    profile_id = created.json()["id"]
    before = created.json()["fingerprint"]["seed"]

    reset = await client.post(f"/api/profiles/{profile_id}/reset-fingerprint")
    assert reset.status_code == 200
    assert reset.json()["fingerprint"]["seed"] != before


@pytest.mark.asyncio
async def test_invalid_browser_settings_are_rejected_not_500(client):
    """browser_settings is a free-form dict, so bad values must read as 422."""
    created = await client.post("/api/profiles", json={"name": "bad"})
    profile_id = created.json()["id"]

    response = await client.put(
        f"/api/profiles/{profile_id}",
        json={"browser_settings": {"os": "macos"}},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_the_webrtc_policy_is_one_of_chromes(client):
    created = await client.post(
        "/api/profiles",
        json={"name": "rtc", "browser_settings": {"webrtc_policy": "disable_non_proxied_udp"}},
    )
    assert created.status_code == 201
    assert created.json()["browser_settings"]["webrtc_policy"] == "disable_non_proxied_udp"

    refused = await client.post(
        "/api/profiles", json={"name": "rtc2", "browser_settings": {"webrtc_policy": "none"}}
    )
    assert refused.status_code == 400


@pytest.mark.asyncio
async def test_profile_statistics_report_the_sessions_that_happened(client):
    """Regression: the endpoint read keys the manager did not return, so it
    always answered zero sessions, no last session and no actions — whatever the
    profile's history actually was.
    """
    created = await client.post("/api/profiles", json={"name": "busy"})
    profile_id = created.json()["id"]
    manager = get_profile_manager()
    for _ in range(3):
        await manager.storage.log_usage(UsageStats(profile_id=profile_id, action="launch_browser"))
    profile = await manager.get_profile(profile_id)
    profile.last_used = datetime(2026, 1, 2, 3, 4, 5)
    await manager.storage.update_profile(profile)

    body = (await client.get(f"/api/profiles/{profile_id}/stats")).json()

    assert body["total_sessions"] == 3
    assert body["last_session"].startswith("2026-01-02T03:04:05")
    assert [a["action"] for a in body["actions"]].count("launch_browser") == 3
    assert body["success_rate"] == 1.0


@pytest.mark.asyncio
async def test_statistics_for_an_unknown_profile_are_a_404(client):
    assert (await client.get("/api/profiles/nope/stats")).status_code == 404


@pytest.mark.asyncio
async def test_an_import_can_name_the_profile_from_the_form(client, tmp_path):
    """Every other part of this request travels in the multipart body.

    A client author reaches for the form field first, and used to get no error
    and no rename because only the query parameter was read.
    """
    created = await client.post("/api/v1/profiles", json={"name": "source"})
    exported = await client.get(f"/api/v1/profiles/{created.json()['id']}/export")
    archive = exported.content

    from_form = await client.post(
        "/api/v1/profiles/import",
        files={"file": ("a.zip", archive, "application/zip")},
        data={"name": "named-by-form"},
    )
    from_query = await client.post(
        "/api/v1/profiles/import?name=named-by-query",
        files={"file": ("a.zip", archive, "application/zip")},
    )

    assert from_form.json()["name"] == "named-by-form"
    assert from_query.json()["name"] == "named-by-query", "the query form must keep working"


@pytest.mark.asyncio
async def test_clearing_geography_rejects_an_empty_id(client):
    """A client building ids from empty form state used to get a cheerful 200
    reporting "" as not_found, rather than being told the request was wrong."""
    refused = await client.post("/api/v1/profiles/clear-geography", json={"profile_ids": [""]})

    assert refused.status_code == 422


@pytest.mark.asyncio
async def test_a_launch_points_downloads_at_the_profiles_own_folder(client):
    import json as _json

    created = await client.post("/api/profiles", json={"name": "downloader"})
    profile_id = created.json()["id"]

    launched = await client.post(f"/api/v1/profiles/{profile_id}/launch", json={"mode": "headless"})

    manager = get_profile_manager()
    folder = manager.downloads_dir(profile_id)
    assert launched.json()["downloads_dir"] == str(folder)
    prefs = manager.profiles_dir / f"profile_{profile_id}" / "Default" / "Preferences"
    assert _json.loads(prefs.read_text())["download"]["default_directory"] == str(folder)


@pytest.mark.asyncio
async def test_downloads_are_listed_fetched_and_deleted(client):
    created = await client.post("/api/profiles", json={"name": "downloader"})
    profile_id = created.json()["id"]
    folder = get_profile_manager().downloads_dir(profile_id)
    folder.mkdir(parents=True)
    (folder / "report.csv").write_bytes(b"a,b\n1,2\n")
    (folder / "big.zip.crdownload").write_bytes(b"partial")

    listed = (await client.get(f"/api/v1/profiles/{profile_id}/downloads")).json()
    assert listed["downloads_dir"] == str(folder)
    assert {f["name"]: f["complete"] for f in listed["files"]} == {
        "report.csv": True,
        "big.zip.crdownload": False,
    }

    fetched = await client.get(f"/api/v1/profiles/{profile_id}/downloads/report.csv")
    assert fetched.status_code == 200
    assert fetched.content == b"a,b\n1,2\n"
    assert "report.csv" in fetched.headers["content-disposition"]

    deleted = await client.delete(f"/api/v1/profiles/{profile_id}/downloads/report.csv")
    assert deleted.status_code == 200
    assert not (folder / "report.csv").exists()


@pytest.mark.asyncio
async def test_a_download_name_cannot_leave_the_folder(client):
    created = await client.post("/api/profiles", json={"name": "downloader"})
    profile_id = created.json()["id"]
    get_profile_manager().downloads_dir(profile_id).mkdir(parents=True)

    for name in ("..%2F..%2Fapi.db", "%2E%2E", ".hidden"):
        response = await client.get(f"/api/v1/profiles/{profile_id}/downloads/{name}")
        assert response.status_code == 404, name


@pytest.mark.asyncio
async def test_downloads_of_an_unknown_profile_are_a_404(client):
    assert (await client.get("/api/v1/profiles/nope/downloads")).status_code == 404


@pytest.mark.asyncio
async def test_purging_a_profile_deletes_its_downloads(client):
    created = await client.post("/api/profiles", json={"name": "gone"})
    profile_id = created.json()["id"]
    folder = get_profile_manager().downloads_dir(profile_id)
    folder.mkdir(parents=True)
    (folder / "file.pdf").write_bytes(b"x")

    await client.delete(f"/api/v1/profiles/{profile_id}")
    purged = await client.delete(f"/api/v1/trash/profiles/{profile_id}", params={"confirm": "true"})

    assert purged.status_code == 200
    assert not folder.exists()


@pytest.mark.asyncio
async def test_a_clone_without_data_starts_logged_out(client):
    created = await client.post(
        "/api/profiles",
        json={"name": "warm", "proxy_config": {"type": "http", "server": "1.2.3.4:8080"}},
    )
    source_id = created.json()["id"]
    manager = get_profile_manager()
    source_dir = manager.profiles_dir / f"profile_{source_id}"
    (source_dir / "Default").mkdir(parents=True, exist_ok=True)
    (source_dir / "Default" / "Cookies").write_bytes(b"logged in")

    with_data = await client.post(f"/api/v1/profiles/{source_id}/clone", json={"new_name": "with"})
    without = await client.post(
        f"/api/v1/profiles/{source_id}/clone", json={"new_name": "without", "copy_data": False}
    )

    assert with_data.status_code == without.status_code == 201
    with_dir = manager.profiles_dir / f"profile_{with_data.json()['id']}"
    without_dir = manager.profiles_dir / f"profile_{without.json()['id']}"
    assert (with_dir / "Default" / "Cookies").read_bytes() == b"logged in"
    assert not (without_dir / "Default" / "Cookies").exists()
    assert without.json()["proxy_config"]["server"] == "1.2.3.4:8080", "settings still copy"
    assert without.json()["fingerprint"]["seed"] != created.json()["fingerprint"]["seed"]

"""Integration tests for the browser-control endpoints, against the fake browser."""

import pytest

from phasmid_pm.api.dependencies import get_profile_manager
from tests.fakes import launch_through_api


@pytest.mark.asyncio
async def test_no_browsers_are_reported_when_none_are_running(client):
    body = (await client.get("/api/browsers/active")).json()

    assert body == {"active_browsers": [], "count": 0}


@pytest.mark.asyncio
async def test_a_running_browser_is_listed_with_its_process(client):
    profile_id, session = await launch_through_api(client, "p1")

    body = (await client.get("/api/browsers/active")).json()

    assert body["count"] == 1
    assert body["active_browsers"][0]["profile_id"] == profile_id
    assert body["active_browsers"][0]["process_id"] == session.process_id
    assert body["active_browsers"][0]["cdp"] is True
    assert body["active_browsers"][0]["started_at"]


@pytest.mark.asyncio
async def test_closing_one_browser_leaves_the_others_running(client):
    first_id, first = await launch_through_api(client, "p1")
    _, second = await launch_through_api(client, "p2")

    result = (await client.post(f"/api/profiles/{first_id}/close")).json()

    assert result["status"] == "closed"
    assert first.process.returncode is not None
    assert second.process.returncode is None
    assert (await client.get("/api/browsers/active")).json()["count"] == 1


@pytest.mark.asyncio
async def test_closing_a_browser_that_is_not_running_is_not_an_error(client):
    """The window may already have been closed by hand; saying so beats a 500."""
    response = await client.post("/api/profiles/nope/close")

    assert response.status_code == 200
    assert response.json()["status"] == "not_running"


@pytest.mark.asyncio
async def test_close_all_closes_every_browser_and_counts_them(client):
    browsers = [(await launch_through_api(client, name))[1] for name in ("p1", "p2", "p3")]

    body = (await client.post("/api/browsers/close-all")).json()

    assert body["closed_count"] == 3
    assert all(b.process.returncode is not None for b in browsers)
    assert (await client.get("/api/browsers/active")).json()["count"] == 0


@pytest.mark.asyncio
async def test_launching_an_unknown_profile_is_a_404(client):
    response = await client.post("/api/profiles/nope/launch", json={"headless": True})

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_a_clone_is_created_as_a_new_profile(client):
    created = (await client.post("/api/profiles", json={"name": "source"})).json()

    cloned = await client.post(f"/api/profiles/{created['id']}/clone", json={"new_name": "copy"})

    assert cloned.status_code == 201
    assert cloned.json()["name"] == "copy"
    assert cloned.json()["id"] != created["id"]
    assert (await client.get(f"/api/profiles/{created['id']}")).status_code == 200


@pytest.mark.asyncio
async def test_cloning_an_unknown_profile_is_a_404(client):
    response = await client.post("/api/profiles/nope/clone", json={"new_name": "copy"})

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_launching_a_profile_leased_elsewhere_is_a_conflict(client):
    """409, not 500: the client can retry once the other holder closes it."""
    created = (await client.post("/api/profiles", json={"name": "leased"})).json()
    manager = get_profile_manager()
    await manager.storage.acquire_lease(created["id"], "other-host:999:c0ffee", 120)

    response = await client.post(f"/api/profiles/{created['id']}/launch", json={"headless": True})

    assert response.status_code == 409
    assert "leased by another holder" in response.json()["detail"]


@pytest.mark.asyncio
async def test_exporting_a_profile_leased_elsewhere_is_a_conflict(client):
    created = (await client.post("/api/profiles", json={"name": "leased"})).json()
    manager = get_profile_manager()
    await manager.storage.acquire_lease(created["id"], "other-host:999:c0ffee", 120)

    response = await client.get(f"/api/profiles/{created['id']}/export")

    assert response.status_code == 409

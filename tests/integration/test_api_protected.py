"""Protected profiles: what protection refuses, and what it leaves alone."""

import pytest

from camoufox_pm.core.models import Profile


async def make(client, **body):
    created = await client.post("/api/v1/profiles", json={"name": "acct", **body})
    assert created.status_code == 201
    return created.json()


@pytest.mark.asyncio
async def test_a_profile_is_unprotected_unless_asked(client):
    assert (await make(client))["protected"] is False
    assert (await make(client, protected=True))["protected"] is True


@pytest.mark.asyncio
async def test_a_protected_profile_cannot_be_moved_to_trash(client):
    profile = await make(client, protected=True)

    refused = await client.delete(f"/api/v1/profiles/{profile['id']}")

    assert refused.status_code == 409
    # Its own code: an agent must be able to tell "off limits" from "busy".
    assert refused.json()["error"]["code"] == "protected"
    assert (await client.get(f"/api/v1/profiles/{profile['id']}")).status_code == 200


@pytest.mark.asyncio
async def test_a_protected_profile_keeps_its_machine(client):
    profile = await make(client, protected=True)

    refused = await client.post(f"/api/v1/profiles/{profile['id']}/reset-fingerprint")

    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "protected"
    after = (await client.get(f"/api/v1/profiles/{profile['id']}")).json()
    assert after["browser_settings"] == profile["browser_settings"]


@pytest.mark.asyncio
async def test_protection_does_not_block_ordinary_edits(client):
    profile = await make(client, protected=True)

    edited = await client.put(
        f"/api/v1/profiles/{profile['id']}",
        json={"name": "renamed", "notes": "2FA on", "row_version": profile["row_version"]},
    )

    assert edited.status_code == 200
    assert edited.json()["name"] == "renamed"
    assert edited.json()["protected"] is True


@pytest.mark.asyncio
async def test_unprotecting_is_the_deliberate_step_that_allows_a_delete(client):
    profile = await make(client, protected=True)

    lifted = await client.put(f"/api/v1/profiles/{profile['id']}", json={"protected": False})
    deleted = await client.delete(f"/api/v1/profiles/{profile['id']}")

    assert lifted.json()["protected"] is False
    assert deleted.status_code == 200


@pytest.mark.asyncio
async def test_an_existing_profile_can_be_protected(client):
    profile = await make(client)

    protected = await client.put(
        f"/api/v1/profiles/{profile['id']}",
        json={"protected": True, "row_version": profile["row_version"]},
    )

    assert protected.json()["protected"] is True
    assert (await client.delete(f"/api/v1/profiles/{profile['id']}")).status_code == 409


@pytest.mark.asyncio
async def test_a_clone_starts_unprotected(client):
    """A copy is a new account; it has earned nothing yet."""
    profile = await make(client, protected=True)

    clone = await client.post(f"/api/v1/profiles/{profile['id']}/clone", json={"new_name": "copy"})

    assert clone.status_code in (200, 201)
    assert clone.json()["protected"] is False


@pytest.mark.asyncio
async def test_the_trash_statement_itself_refuses_a_protected_profile(storage):
    """Protecting a profile while a delete is in flight must not lose the race,
    so the guard lives in the UPDATE rather than only in a read before it."""
    profile = Profile(name="acct", protected=True)
    await storage.save_profile(profile)

    assert await storage.set_profile_trashed(profile.id, True) is False
    assert (await storage.get_profile(profile.id)).deleted_at is None

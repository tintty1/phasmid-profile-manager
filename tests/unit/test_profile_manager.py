"""Cloning, deleting and launching: the operations that move data around.

CRUD over the API is covered in tests/integration; these are the parts of the
manager that touch the filesystem or a profile's identity.
"""

import json
import os
import shutil
import stat
from pathlib import Path

import pytest

from phasmid_pm.core.browser_session import BrowserLaunchError
from phasmid_pm.core.leases import ProfileLocked
from tests.fakes import fake_pin

PINNED = fake_pin("windows", seed="77" * 16, public_ip="203.0.113.10")


async def pin(profile_manager, profile):
    """Give a profile a pinned machine, as its first launch would."""
    profile.fingerprint = dict(PINNED)
    await profile_manager.storage.update_profile(profile)
    return profile


# -- Cloning --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_clone_carries_the_browser_data_into_a_directory_of_its_own(
    profile_manager, tmp_path
):
    """Cloning exists to reuse a warmed-up profile, which is that directory."""
    source = await profile_manager.create_profile(name="warm")
    source_dir = Path(source.get_storage_path(str(profile_manager.profiles_dir)))
    (source_dir / "cookies.sqlite").write_bytes(b"logged in")

    clone = await profile_manager.clone_profile(source.id, "warm copy")

    assert clone is not None and clone.id != source.id
    clone_dir = Path(clone.get_storage_path(str(profile_manager.profiles_dir)))
    assert clone_dir != source_dir
    assert (clone_dir / "cookies.sqlite").read_bytes() == b"logged in"
    assert (source_dir / "cookies.sqlite").exists()


@pytest.mark.asyncio
async def test_a_clone_is_a_different_machine(profile_manager):
    """Regression: the pinned fingerprint was copied along with everything else.

    The clone then reported the same GPU, screen, core count and noise seeds as
    its source — two profiles that are provably one machine, which is the single
    thing this product exists to prevent.
    """
    source = await pin(profile_manager, await profile_manager.create_profile(name="source"))

    clone = await profile_manager.clone_profile(source.id, "copy")

    assert clone is not None
    assert clone.fingerprint is not None
    assert clone.fingerprint["seed"] != PINNED["seed"]
    assert (await profile_manager.get_profile(source.id)).fingerprint == PINNED


@pytest.mark.asyncio
async def test_a_clone_can_be_asked_to_keep_the_machine(profile_manager):
    """Deliberately the same computer — moving a profile between installs, say."""
    source = await pin(profile_manager, await profile_manager.create_profile(name="source"))

    clone = await profile_manager.clone_profile(source.id, "copy", regenerate_fingerprint=False)

    assert clone is not None
    assert clone.fingerprint == PINNED
    assert clone.browser_settings == source.browser_settings


@pytest.mark.asyncio
async def test_cloning_an_unknown_profile_returns_nothing(profile_manager):
    assert await profile_manager.clone_profile("nope", "copy") is None


# -- Deleting -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deleting_a_profile_retains_its_directory_in_trash(profile_manager):
    profile = await profile_manager.create_profile(name="gone")
    directory = Path(profile.get_storage_path(str(profile_manager.profiles_dir)))
    (directory / "cookies.sqlite").write_bytes(b"session")

    assert await profile_manager.delete_profile(profile.id) is True
    assert (directory / "cookies.sqlite").read_bytes() == b"session"
    assert await profile_manager.get_profile(profile.id) is None
    assert [p.id for p in await profile_manager.list_trashed_profiles()] == [profile.id]


@pytest.mark.asyncio
async def test_the_data_can_be_kept_when_the_profile_is_deleted(profile_manager):
    """Legacy remove_data=False also preserves the recoverable profile."""
    profile = await profile_manager.create_profile(name="gone")
    directory = Path(profile.get_storage_path(str(profile_manager.profiles_dir)))
    (directory / "cookies.sqlite").write_bytes(b"session")

    await profile_manager.delete_profile(profile.id, remove_data=False)

    assert (directory / "cookies.sqlite").read_bytes() == b"session"


@pytest.mark.asyncio
async def test_a_profile_whose_directory_is_already_gone_still_deletes(profile_manager):
    """Otherwise a half-removed profile could never be cleared from the list."""
    profile = await profile_manager.create_profile(name="gone")
    shutil.rmtree(profile.get_storage_path(str(profile_manager.profiles_dir)))

    assert await profile_manager.delete_profile(profile.id) is True


@pytest.mark.asyncio
async def test_deleting_an_unknown_profile_reports_it_rather_than_raising(profile_manager):
    assert await profile_manager.delete_profile("nope") is False


# -- Groups ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deleting_a_group_ungroups_its_profiles_and_keeps_them(profile_manager):
    """A group is a label. Deleting one must never take the profiles with it."""
    group = await profile_manager.create_group("Social")
    first = await profile_manager.create_profile(name="a", group=group["id"])
    second = await profile_manager.create_profile(name="b", group=group["id"])

    assert await profile_manager.delete_group(group["id"]) is True

    remaining = await profile_manager.list_profiles()
    assert {p.id for p in remaining} == {first.id, second.id}
    assert [p.group for p in remaining] == [None, None]


@pytest.mark.asyncio
async def test_deleting_an_unknown_group_reports_it(profile_manager):
    assert await profile_manager.delete_group("nope") is False


@pytest.mark.asyncio
async def test_a_group_reports_how_many_profiles_it_holds(profile_manager):
    group = await profile_manager.create_group("Social", description="socials")
    await profile_manager.create_profile(name="a", group=group["id"])
    await profile_manager.create_profile(name="b")

    assert (await profile_manager.get_group(group["id"]))["profile_count"] == 1
    assert [g["profile_count"] for g in await profile_manager.list_groups()] == [1]


@pytest.mark.asyncio
async def test_bulk_update_counts_only_the_profiles_that_exist(profile_manager):
    first = await profile_manager.create_profile(name="a")
    second = await profile_manager.create_profile(name="b")

    updated = await profile_manager.bulk_update_profiles(
        [first.id, "missing", second.id], {"status": "inactive"}
    )

    assert updated == 2
    assert [p.status for p in await profile_manager.list_profiles()] == ["inactive", "inactive"]


# -- Launching ------------------------------------------------------------------
#
# Real launches of the fake browser binary from the phasmid fixture: the command
# line, the session files and the location move are the production code paths.


def launched(profile_manager, profile_id: str) -> dict:
    """What the fake browser saw: its argv and environment."""
    storage = Path(profile_manager.profiles_dir) / f"profile_{profile_id}"
    return json.loads((storage / "argv.json").read_text())


def switch(argv: list[str], name: str) -> str | None:
    return next((a.split("=", 1)[1] for a in argv if a.startswith(f"{name}=")), None)


@pytest.mark.asyncio
async def test_launching_an_unknown_profile_says_so(profile_manager):
    with pytest.raises(ValueError, match="not found"):
        await profile_manager.launch_browser("nope")


@pytest.mark.asyncio
async def test_a_launch_hands_the_browser_its_machine(profile_manager, phasmid):
    profile = await profile_manager.create_profile(
        name="p", browser_settings={"webrtc_policy": "disable_non_proxied_udp"}
    )

    result = await profile_manager.launch_browser(profile.id, mode="headless")

    assert result["status"] == "launched"
    argv = launched(profile_manager, profile.id)["argv"]
    assert argv[0] == str(phasmid.installation.binary)
    assert switch(argv, "--user-data-dir") == str(
        Path(profile_manager.profiles_dir) / f"profile_{profile.id}"
    )
    assert "--remote-debugging-port=0" in argv
    assert "--password-store=basic" in argv
    assert "--headless" in argv
    handed = Path(switch(argv, "--phasmid-profile"))
    assert stat.S_IMODE(handed.stat().st_mode) == 0o600
    sent = json.loads(handed.read_text())
    stored = (await profile_manager.get_profile(profile.id)).fingerprint
    assert sent["seed"] == stored["seed"]
    assert sent["network"]["webrtc"]["ip_handling_policy"] == "disable_non_proxied_udp"


@pytest.mark.asyncio
async def test_the_first_launch_places_the_machine_at_the_exit(profile_manager, phasmid):
    phasmid.installed = False
    profile = await profile_manager.create_profile(name="p")
    assert profile.fingerprint is None
    phasmid.installed = True
    phasmid.exit_ip = "203.0.113.10"

    result = await profile_manager.launch_browser(profile.id, mode="headless")

    stored = await profile_manager.get_profile(profile.id)
    assert result["launch"]["exit_ip"] == "203.0.113.10"
    assert stored.fingerprint["network"]["webrtc"]["public_ip"] == "203.0.113.10"
    assert stored.fingerprint["locale"]["timezone"] == "Europe/Berlin"
    assert stored.fingerprint["locale"]["languages"][0] == "de-DE"
    assert stored.last_used is not None


@pytest.mark.asyncio
async def test_a_new_exit_moves_the_machine_and_keeps_its_hardware(profile_manager, phasmid):
    profile = await profile_manager.create_profile(name="p")
    phasmid.exit_ip = "203.0.113.10"
    await profile_manager.launch_browser(profile.id, mode="headless")
    await profile_manager.close_browser(profile.id)
    berlin = (await profile_manager.get_profile(profile.id)).fingerprint

    phasmid.exit_ip = "203.0.113.20"
    result = await profile_manager.launch_browser(profile.id, mode="headless")

    tokyo = (await profile_manager.get_profile(profile.id)).fingerprint
    assert result["launch"]["moved"] is True
    assert tokyo["locale"]["timezone"] == "Asia/Tokyo"
    assert tokyo["network"]["webrtc"]["public_ip"] == "203.0.113.20"
    for section in ("seed", "os", "hardware", "display", "gpu", "fonts"):
        assert tokyo[section] == berlin[section], f"{section} must not change with the exit"
    # The move regenerates from the pin's own seed, OS and GPU.
    request = phasmid.genprofile.generated()[-1]
    assert (request["seed"], request["os"]) == (berlin["seed"], berlin["os"]["family"])
    assert request["gpu"] == f"{berlin['gpu']['vendor_id']}:{berlin['gpu']['device_id']}"


@pytest.mark.asyncio
async def test_the_same_exit_does_not_touch_the_machine(profile_manager, phasmid):
    profile = await profile_manager.create_profile(name="p")
    await profile_manager.launch_browser(profile.id, mode="headless")
    await profile_manager.close_browser(profile.id)
    generated = len(phasmid.genprofile.generated())

    result = await profile_manager.launch_browser(profile.id, mode="headless")

    assert result["launch"]["moved"] is False
    assert len(phasmid.genprofile.generated()) == generated


@pytest.mark.asyncio
async def test_a_stated_timezone_wins_and_keeps_the_exit_address(profile_manager, phasmid):
    """WebRTC's UDP bypasses the proxy, so the address must stay the exit's."""
    profile = await profile_manager.create_profile(
        name="p", browser_settings={"timezone": "Asia/Tokyo", "languages": ["ja", "en"]}
    )
    phasmid.exit_ip = "203.0.113.10"

    await profile_manager.launch_browser(profile.id, mode="headless")

    pin = (await profile_manager.get_profile(profile.id)).fingerprint
    assert pin["locale"]["timezone"] == "Asia/Tokyo"
    assert pin["locale"]["languages"] == ["ja", "en"]
    assert pin["network"]["webrtc"]["public_ip"] == "203.0.113.10"


@pytest.mark.asyncio
async def test_an_unreachable_proxy_starts_nothing_and_frees_the_profile(profile_manager, phasmid):
    profile = await profile_manager.create_profile(
        name="p", proxy_config={"type": "http", "server": "127.0.0.1:9"}
    )
    phasmid.exit_ip = None

    with pytest.raises(BrowserLaunchError, match="proxy did not answer"):
        await profile_manager.launch_browser(profile.id, mode="headless")

    assert not profile_manager.browser_sessions.is_running(profile.id)
    assert await profile_manager.storage.get_lease(profile.id) == (None, None)


@pytest.mark.asyncio
async def test_without_a_proxy_an_offline_launch_keeps_the_machine_where_it_was(
    profile_manager, phasmid
):
    profile = await profile_manager.create_profile(name="p")
    before = profile.fingerprint
    phasmid.exit_ip = None

    result = await profile_manager.launch_browser(profile.id, mode="headless")

    assert result["status"] == "launched"
    assert (await profile_manager.get_profile(profile.id)).fingerprint == before


@pytest.mark.asyncio
async def test_a_proxy_goes_through_proxyroute_with_its_credentials_in_a_private_file(
    profile_manager, phasmid
):
    proxyroute = phasmid.installation.proxyroute
    proxyroute.parent.mkdir(parents=True)
    # A stand-in for proxyroute: run the command after "--", as it does.
    proxyroute.write_text(
        "import os, sys\ni = sys.argv.index('--')\nos.execv(sys.argv[i + 1], sys.argv[i + 1:])\n"
    )
    profile = await profile_manager.create_profile(
        name="p",
        proxy_config={
            "type": "socks5",
            "server": "proxy.example:1080",
            "username": "alice",
            "password": "p@ss:word",
        },
    )

    result = await profile_manager.launch_browser(profile.id, mode="headless")

    argv = result["launch"]["argv"]
    assert argv[1] == str(proxyroute)
    assert not any("p@ss" in arg or "p%40ss" in arg for arg in argv), "never on the command line"
    session = profile_manager.browser_sessions.active_sessions[profile.id]
    args_file = session.run_dir / "proxy-args"
    assert stat.S_IMODE(os.stat(args_file).st_mode) == 0o600
    assert (
        args_file.read_text() == "--proxy-default=socks5://alice:p%40ss%3Aword@proxy.example:1080\n"
    )

    await profile_manager.close_browser(profile.id)
    assert not args_file.exists(), "credentials go with the session"


@pytest.mark.asyncio
async def test_socks4_goes_to_chrome_directly(profile_manager):
    profile = await profile_manager.create_profile(
        name="p", proxy_config={"type": "socks4", "server": "proxy.example:1080"}
    )

    await profile_manager.launch_browser(profile.id, mode="headless")

    argv = launched(profile_manager, profile.id)["argv"]
    assert "--proxy-server=socks4://proxy.example:1080" in argv


@pytest.mark.asyncio
async def test_extra_switches_are_appended(profile_manager):
    profile = await profile_manager.create_profile(name="p")

    await profile_manager.launch_browser(profile.id, mode="headless", extra_args=["--lang=de"])

    assert "--lang=de" in launched(profile_manager, profile.id)["argv"]


@pytest.mark.asyncio
async def test_a_second_launch_does_not_open_a_second_browser(profile_manager):
    profile = await profile_manager.create_profile(name="p")

    first = await profile_manager.launch_browser(profile.id, mode="headless")
    again = await profile_manager.launch_browser(profile.id, mode="headless")

    assert again["status"] == "already_running"
    assert again["process_id"] == first["process_id"]


@pytest.mark.asyncio
async def test_a_browser_failing_to_start_frees_the_profile(profile_manager, monkeypatch):
    monkeypatch.setenv("FAKE_CHROME", "crash")
    profile = await profile_manager.create_profile(name="p")

    with pytest.raises(BrowserLaunchError, match="profile refused"):
        await profile_manager.launch_browser(profile.id, mode="headless")

    assert await profile_manager.storage.get_lease(profile.id) == (None, None)


@pytest.mark.asyncio
async def test_closing_a_browser_that_is_not_running_says_so(profile_manager):
    result = await profile_manager.close_browser("nope")

    assert result["status"] == "not_running"


@pytest.mark.asyncio
async def test_regenerating_gives_a_new_seed(profile_manager):
    profile = await pin(profile_manager, await profile_manager.create_profile(name="p"))

    rotated = await profile_manager.rotate_profile_fingerprint(profile.id)

    assert rotated is not None and rotated.fingerprint["seed"] != PINNED["seed"]


@pytest.mark.asyncio
async def test_regenerating_without_the_browser_leaves_it_to_the_first_launch(
    profile_manager, phasmid
):
    profile = await pin(profile_manager, await profile_manager.create_profile(name="p"))
    phasmid.installed = False

    rotated = await profile_manager.rotate_profile_fingerprint(profile.id)

    assert rotated is not None and rotated.fingerprint is None


# -- Orphans --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_browser_left_by_a_dead_instance_is_closed_at_startup(profile_manager):
    profile = await profile_manager.create_profile(name="p")
    await profile_manager.launch_browser(profile.id, mode="headless")
    session = profile_manager.browser_sessions.active_sessions.pop(profile.id)
    session.monitor_task.cancel()
    # As if the holder had died: a pid on this host that no longer exists.
    dead = "{}:999999999:x".format(__import__("socket").gethostname())
    profile_manager.storage.db._connection.execute(
        "UPDATE profiles SET locked_by = ? WHERE id = ?", (dead, profile.id)
    )

    assert await profile_manager.reap_orphans() == 1

    assert await session.process.wait() is not None
    assert not session.run_dir.exists()
    assert await profile_manager.storage.get_lease(profile.id) == (None, None)


@pytest.mark.asyncio
async def test_a_browser_another_live_instance_runs_is_left_alone(profile_manager):
    profile = await profile_manager.create_profile(name="p")
    await profile_manager.launch_browser(profile.id, mode="headless")
    session = profile_manager.browser_sessions.active_sessions[profile.id]
    # The parent of this test process stands in for another live instance.
    other = "{}:{}:y".format(__import__("socket").gethostname(), os.getppid())
    profile_manager.storage.db._connection.execute(
        "UPDATE profiles SET locked_by = ? WHERE id = ?", (other, profile.id)
    )

    assert await profile_manager.reap_orphans() == 0

    assert session.process.returncode is None
    with pytest.raises(ProfileLocked):
        await profile_manager.launch_browser(profile.id, mode="headless")

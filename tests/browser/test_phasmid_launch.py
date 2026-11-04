"""Real Phasmid launches: what a page sees is the pinned machine, every time.

Opt-in (``-m browser``) and skipped unless PPM_PHASMID_BINARY and
PPM_PHASMID_SOURCE name a local build and checkout; see conftest.py.
"""

import asyncio

import pytest
from playwright.async_api import async_playwright

from phasmid_pm.config import get_settings
from phasmid_pm.core import browser_install, proxy_check
from phasmid_pm.core.database import StorageManager
from phasmid_pm.core.profile_manager import ProfileManager
from tests.browser.support import timezone_a_page_sees

pytestmark = [pytest.mark.browser, pytest.mark.asyncio]

# In DB-IP City Lite: Mountain View, America/Los_Angeles.
EXIT = "8.8.8.8"

PROBE = """() => ({
    platform: navigator.platform,
    ua: navigator.userAgent,
    cores: navigator.hardwareConcurrency,
    memory: navigator.deviceMemory,
    languages: navigator.languages,
    screen: [screen.width, screen.height],
    webdriver: navigator.webdriver,
})"""


@pytest.fixture
async def manager(tmp_path, monkeypatch):
    monkeypatch.setenv("PPM_DB_PATH", str(tmp_path / "profiles.db"))
    get_settings.cache_clear()
    if not browser_install.installed_browser()[0]:
        get_settings.cache_clear()
        pytest.skip("needs PPM_PHASMID_BINARY, PPM_PHASMID_SOURCE and the DB-IP cache")

    async def exit_here(proxy, timeout=proxy_check.DEFAULT_TIMEOUT):
        return EXIT, 1

    monkeypatch.setattr(proxy_check, "resolve_exit_ip", exit_here)
    storage = StorageManager(str(tmp_path / "profiles.db"))
    profiles = ProfileManager(storage, str(tmp_path))
    await profiles.initialize()
    yield profiles
    await profiles.browser_sessions.close_all()
    await storage.close()
    get_settings.cache_clear()


async def what_a_page_sees(manager, profile_id, url):
    session = manager.browser_sessions.active_sessions[profile_id]
    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(session.cdp_endpoint)
        context = browser.contexts[0]
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(url)
        seen = await page.evaluate(PROBE)
        seen["timezone"] = await timezone_a_page_sees(page)
        await browser.close()  # over CDP this only disconnects
    return seen


async def test_a_page_sees_the_pinned_machine_at_its_exit(manager, local_sites):
    profile = await manager.create_profile("linux", browser_settings={"os": "linux"})

    await manager.launch_browser(profile.id, mode="headless")
    seen = await what_a_page_sees(manager, profile.id, local_sites.first)

    pin = (await manager.get_profile(profile.id)).fingerprint
    assert pin["network"]["webrtc"]["public_ip"] == EXIT
    assert pin["locale"]["timezone"] == "America/Los_Angeles"
    assert seen["platform"] == pin["os"]["navigator_platform"]
    assert seen["cores"] == pin["hardware"]["concurrency"]
    assert seen["memory"] == pin["hardware"]["device_memory"]
    assert seen["languages"] == pin["locale"]["languages"]
    assert seen["screen"] == [pin["display"]["width"], pin["display"]["height"]]
    assert seen["timezone"] == "America/Los_Angeles"
    assert seen["webdriver"] is False
    assert "Headless" not in seen["ua"]


async def test_the_same_machine_comes_back_on_the_next_launch(manager, local_sites):
    profile = await manager.create_profile("windows", browser_settings={"os": "windows"})

    await manager.launch_browser(profile.id, mode="headless")
    first = await what_a_page_sees(manager, profile.id, local_sites.first)
    await manager.close_browser(profile.id)
    await manager.launch_browser(profile.id, mode="headless")
    second = await what_a_page_sees(manager, profile.id, local_sites.first)

    assert first == second
    assert first["platform"] == "Win32"


async def test_closing_the_browser_from_inside_is_noticed(manager):
    profile = await manager.create_profile("closer")
    await manager.launch_browser(profile.id, mode="headless")
    session = manager.browser_sessions.active_sessions[profile.id]

    await session._close_over_cdp()  # noqa: SLF001 - what a person closing the window does
    for _ in range(100):
        if not manager.browser_sessions.is_running(profile.id):
            break
        await asyncio.sleep(0.1)

    assert not manager.browser_sessions.is_running(profile.id)
    assert await manager.storage.get_lease(profile.id) == (None, None)
    assert not session.run_dir.exists()

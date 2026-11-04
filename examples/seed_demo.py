#!/usr/bin/env python3
"""Seed a local database with a few demo profiles.

Run from the repository root:

    uv run python examples/seed_demo.py

This creates ``demo_data/profiles.db`` with a group and several profiles so you
can explore the API and web interface with realistic data.
"""

import asyncio

from phasmid_pm.core.database import StorageManager
from phasmid_pm.core.models import ProxyConfig, ProxyType
from phasmid_pm.core.profile_manager import ProfileManager

# Location follows each profile's exit address at launch; these differ in the
# machine (OS) and in a stated language list.
DEMO_PROFILES = [
    {"name": "US shopper", "browser_settings": {"os": "windows"}},
    {"name": "UK researcher", "browser_settings": {"os": "linux", "languages": ["en-GB", "en"]}},
    {"name": "DE tester", "browser_settings": {"os": "linux", "languages": ["de-DE", "de", "en"]}},
]


async def main() -> None:
    storage = StorageManager("demo_data/profiles.db")
    await storage.initialize()
    manager = ProfileManager(storage, "demo_data")
    await manager.initialize()

    group = await manager.create_group("Demo", "Profiles created by seed_demo.py")

    for entry in DEMO_PROFILES:
        profile = await manager.create_profile(
            name=entry["name"],
            group=group["id"],
            browser_settings=entry["browser_settings"],
        )
        print(f"Created profile {profile.name} ({profile.id})")

    # One profile with a proxy to show the proxy fields.
    await manager.create_profile(
        name="Proxied profile",
        group=group["id"],
        proxy_config=ProxyConfig(
            type=ProxyType.HTTP, server="proxy.example.com:8080", username="user", password="pass"
        ).model_dump(),
    )

    profiles = await manager.list_profiles()
    print(f"\nSeeded {len(profiles)} profiles in demo_data/profiles.db")
    await storage.close()


if __name__ == "__main__":
    asyncio.run(main())

"""Browser profile manager."""

import asyncio
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from phasmid_profile_manager.config import get_settings

from . import browser_install, downloads, machine, profile_archive, proxy_check
from .browser_session import BrowserLaunchError, BrowserSessionManager, reap_orphan
from .database import StaleWriteError, StorageManager
from .download_watch import DownloadRegistry
from .launch import build_plan
from .leases import ProfileLocked, holder_alive, make_lease_holder
from .models import (
    BrowserSettings,
    Profile,
    ProfileGroup,
    ProfileStatus,
    ProxyConfig,
    ProxyType,
    UsageStats,
    generate_profile_id,
)
from .profile_operations import (
    ProfileProtected,
    profile_data_path,
    profile_operation,
    run_file_operation,
)
from .vnc import VncDisplays


class ProfileManager:
    """Manage browser profiles: CRUD, cloning, export/import, groups."""

    def __init__(self, storage_manager: StorageManager, data_dir: str = "data"):
        self.storage = storage_manager
        self.data_dir = Path(data_dir)
        self.profiles_dir = self.data_dir / "profiles"
        # Per-session files (the profile JSON a browser reads, proxy arguments).
        self.run_dir = self.data_dir / "run"
        # Downloads CDP clients start, watched by the relay (core/download_watch.py).
        self.download_registry = DownloadRegistry()
        # Profile id -> CDP relays attached now, so the last one to leave can
        # hand downloads back to the profile's preference.
        self.cdp_relays: dict[str, int] = {}

        # Active browser sessions are owned by a dedicated manager, which also
        # renews this process's leases for as long as its browsers are open.
        self.lease_holder = make_lease_holder()
        self.browser_sessions = BrowserSessionManager(
            storage_manager, self.lease_holder, VncDisplays(enabled=get_settings().vnc)
        )

        self.profiles_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"Initialized ProfileManager with data directory: {self.data_dir}")

    async def initialize(self):
        """Initialize the profile manager and its database."""
        await self.storage.initialize()
        await self.reap_orphans()
        self.browser_sessions.start_heartbeat()
        logger.info("ProfileManager initialized")

    async def reap_orphans(self) -> int:
        """Close browsers an earlier run of this app left behind; returns how many.

        A run directory exists only while its browser is open, so one found at
        startup belongs to an instance that died without closing it. Skipped
        while the profile's lease is held by an instance that is still alive:
        another app (the CLI beside the web UI) may be running it.
        """
        if not self.run_dir.is_dir():
            return 0
        count = 0
        for run_dir in self.run_dir.iterdir():
            if not run_dir.is_dir():
                continue
            lease = await self.storage.get_lease(run_dir.name)
            if lease is not None and holder_alive(*lease):
                continue
            if reap_orphan(run_dir):
                count += 1
                logger.warning(f"Closed a browser left running for profile {run_dir.name}")
            if lease is not None and lease[0]:
                await self.storage.release_lease(run_dir.name, lease[0])
        return count

    async def _new_machine(self, settings: BrowserSettings) -> dict[str, Any] | None:
        """A machine for these settings now, or None to pin one on the first launch.

        Generation needs the Phasmid source tree; without it (the browser is not
        installed yet) the profile is created unpinned and its first launch
        generates the machine, exactly as for an imported spreadsheet row.
        """
        if not browser_install.installed_browser()[0]:
            return None
        return await machine.generate(settings)

    async def create_profile(
        self,
        name: str,
        group: str | None = None,
        browser_settings: dict[str, Any] | BrowserSettings | None = None,
        proxy_config: dict[str, Any] | None = None,
        generate_fingerprint: bool = True,
        notes: str | None = None,
        status: ProfileStatus | str | None = None,
        protected: bool = False,
    ) -> Profile:
        """Create a new profile.

        With ``generate_fingerprint`` the machine is generated now (when the
        browser is installed), so the profile can be inspected before it ever
        runs; otherwise its first launch generates it. Either way the first
        launch moves its location group to the exit address.
        """
        logger.info(f"Creating new profile: {name}")

        # Notes belong to the user; the creation time is already in created_at,
        # so it is not stamped into the field the user types into.
        profile = Profile(name=name, group=group, notes=notes, protected=protected)
        if status:
            # Raises for an unknown value rather than falling back to active: a
            # bulk import is where a typo would otherwise reactivate a fleet of
            # profiles their owner had deliberately parked.
            profile.status = ProfileStatus(status)

        if isinstance(browser_settings, BrowserSettings):
            profile.browser_settings = browser_settings
        elif isinstance(browser_settings, dict):
            # Rebuilt through the model rather than setattr, so values are
            # validated. Keys the caller omitted keep their default.
            profile.browser_settings = BrowserSettings(
                **{**profile.browser_settings.model_dump(), **browser_settings}
            )

        if proxy_config:
            profile.proxy = ProxyConfig(**proxy_config)

        if generate_fingerprint:
            profile.fingerprint = await self._new_machine(profile.browser_settings)

        # Create the profile data directory
        profile_dir = Path(profile.get_storage_path(str(self.profiles_dir)))
        profile_dir.mkdir(parents=True, exist_ok=True)

        # Save the profile to the database
        await self.storage.save_profile(profile)

        # Log profile creation
        await self.storage.log_usage(
            UsageStats(
                profile_id=profile.id,
                action="create_profile",
                details={"name": name, "group": group},
            )
        )

        logger.info(f"Profile '{name}' created with ID: {profile.id}")
        return profile

    async def get_profile(self, profile_id: str) -> Profile | None:
        """Get a profile by ID."""
        profile = await self.storage.get_profile(profile_id)
        if profile:
            if profile.storage_path and not Path(profile.storage_path).is_absolute():
                try:
                    profile.storage_path = str(profile_data_path(self, profile))
                except ValueError:
                    # Let the record remain editable; file operations and
                    # launches validate and refuse an unsafe directory.
                    pass
            logger.debug(f"Loaded profile: {profile.name} ({profile_id})")
        else:
            logger.warning(f"Profile with ID {profile_id} not found")
        return profile

    async def update_profile(
        self,
        profile_id: str,
        updates: dict[str, Any],
        expected_row_version: int | None = None,
    ) -> Profile | None:
        """Update a profile, refusing to overwrite a save we did not see.

        ``expected_row_version`` is the version the *caller* read — a client
        that loaded an edit form minutes ago passes the version it was shown,
        and a save that landed since makes this raise ``StaleWriteError``
        instead of reverting it. Omitted, the guard falls back to the version
        read here, which still catches two requests interleaving but cannot
        know about the edit the client never saw.
        """
        logger.info(f"Updating profile {profile_id}")

        profile = await self.get_profile(profile_id)
        if not profile:
            return None
        guard = profile.row_version if expected_row_version is None else expected_row_version
        if guard != profile.row_version:
            # Stale before a single field is applied: the client is editing a
            # version of this profile that no longer exists.
            raise StaleWriteError(profile_id, expected_row_version)

        proxy_before = profile.proxy
        settings_before = profile.browser_settings.model_copy()

        # Update profile fields
        for key, value in updates.items():
            # Map proxy_config -> proxy for API compatibility
            if key == "proxy_config":
                key = "proxy"

            if hasattr(profile, key):
                # Coerce status string to enum
                if key == "status" and isinstance(value, str):
                    value = ProfileStatus(value)
                # Coerce proxy dict to ProxyConfig
                elif key == "proxy" and value is not None:
                    if isinstance(value, dict):
                        value = ProxyConfig(**value)
                elif key == "proxy" and value is None:
                    value = None
                # Coerce browser_settings dict to BrowserSettings
                elif key == "browser_settings" and value is not None:
                    if isinstance(value, dict):
                        value = BrowserSettings(**value)
                setattr(profile, key, value)

        # An answer from the proxy that was there says nothing about the one that
        # is there now — including "no proxy at all", which is a different exit
        # address, not the absence of one. Drop it rather than show it as current.
        if "proxy_config" in updates or "proxy" in updates:
            if profile.proxy != proxy_before:
                profile.proxy_check = None

        # A stated timezone or language list, set or cleared, changes where the
        # machine should be; forgetting its placement makes the next launch
        # place it again, even at the same exit.
        if profile.fingerprint and (
            profile.browser_settings.timezone != settings_before.timezone
            or profile.browser_settings.languages != settings_before.languages
        ):
            profile.fingerprint = machine.forget_placement(profile.fingerprint)

        # Changing the OS of a pinned profile is not refused: the setting is not
        # what a page sees while a pin exists, so nothing breaks, and refusing
        # would block a legitimate edit made on the way to regenerating. But it
        # does leave the profile saying two things, so it is worth a line in the
        # log; the profile response reports it as os_mismatch for the UI.
        if "browser_settings" in updates and profile.fingerprint:
            pinned_os = machine.pinned_os(profile.fingerprint)
            if pinned_os and pinned_os != profile.browser_settings.os:
                logger.warning(
                    f"Profile {profile_id} is now set to {profile.browser_settings.os} "
                    f"while its pinned machine is {pinned_os}; the setting has no effect "
                    "until the machine is regenerated"
                )

        profile.updated_at = datetime.now()

        # Version-checked: a save that landed while this edit was being made
        # must surface as a conflict, not quietly replace the other one.
        await self.storage.update_profile(profile, expected_row_version=guard)

        # Log the update
        await self.storage.log_usage(
            UsageStats(
                profile_id=profile_id,
                action="update_profile",
                details={"updated_fields": list(updates.keys())},
            )
        )

        logger.info(f"Profile {profile_id} updated")
        return profile

    async def delete_profile(self, profile_id: str, remove_data: bool = True) -> bool:
        """Move a profile into recoverable trash; legacy remove_data is ignored."""
        if not await self.get_profile(profile_id):
            return False
        async with profile_operation(self, profile_id) as profile:
            if profile.protected:
                raise ProfileProtected(profile_id, "moved to Trash")
            if await self.storage.set_profile_trashed(profile_id, True):
                return True
            # Refused by the statement's own guard: protected since we read it.
            current = await self.get_profile(profile_id)
            if current is not None and current.protected:
                raise ProfileProtected(profile_id, "moved to Trash")
            return False

    def downloads_dir(self, profile_id: str) -> Path:
        """Where the profile's browser saves downloads (core/downloads.py)."""
        return downloads.downloads_dir(self.data_dir, profile_id)

    async def list_trashed_profiles(self) -> list[Profile]:
        return await self.storage.list_profiles({"trashed": True})

    async def restore_trashed_profile(self, profile_id: str) -> Profile | None:
        profile = await self.storage.get_profile(profile_id, include_deleted=True)
        if not profile or not profile.deleted_at:
            return None
        async with profile_operation(self, profile_id, include_deleted=True):
            await self.storage.set_profile_trashed(profile_id, False)
        # Schedules stay paused until the user deliberately enables them again.
        return await self.get_profile(profile_id)

    async def permanently_delete_profile(self, profile_id: str) -> bool:
        profile = await self.storage.get_profile(profile_id, include_deleted=True)
        if not profile or not profile.deleted_at:
            return False
        async with profile_operation(self, profile_id, include_deleted=True) as profile:
            if not profile.deleted_at:
                return False
            if profile.protected:
                raise ProfileProtected(profile_id, "permanently deleted")
            path = profile_data_path(self, profile)
            if path.exists():
                await run_file_operation(shutil.rmtree, path)
            folder = self.downloads_dir(profile_id)
            if folder.exists():
                await run_file_operation(shutil.rmtree, folder)
            self.download_registry.forget(profile_id)
            return await self.storage.delete_profile(profile_id)

    async def list_profiles(
        self,
        group: str | None = None,
        status: ProfileStatus | None = None,
        filters: dict[str, Any] | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[Profile]:
        """List profiles with optional filtering."""
        # Merge the legacy parameters with the filters dict
        combined_filters = {}
        if group:
            combined_filters["group"] = group
        if status:
            combined_filters["status"] = status
        if filters:
            combined_filters.update(filters)

        profiles = await self.storage.list_profiles(combined_filters, limit, offset)
        logger.debug(f"Loaded {len(profiles)} profiles")
        return profiles

    async def clone_profile(
        self,
        source_id: str,
        new_name: str,
        regenerate_fingerprint: bool = True,
        copy_data: bool = True,
    ) -> Profile | None:
        if not await self.get_profile(source_id):
            return None
        async with profile_operation(self, source_id):
            return await self._clone_profile_unlocked(
                source_id, new_name, regenerate_fingerprint, copy_data
            )

    async def _clone_profile_unlocked(
        self,
        source_id: str,
        new_name: str,
        regenerate_fingerprint: bool = True,
        copy_data: bool = True,
    ) -> Profile | None:
        """Clone a profile.

        ``copy_data`` copies the browser data directory (cookies, logins,
        storage, history); without it the clone starts as a fresh browser with
        the source's settings and proxy. ``regenerate_fingerprint`` gives it a
        new machine. Downloads are never copied.
        """
        logger.info(f"Cloning profile {source_id} -> {new_name}")

        source_profile = await self.get_profile(source_id)
        if not source_profile:
            return None

        # Create a copy of the profile
        profile_data = source_profile.model_dump()
        profile_data.pop("id")  # Drop the ID so a new one is generated
        profile_data["name"] = new_name
        profile_data["created_at"] = datetime.now()
        profile_data["updated_at"] = datetime.now()
        profile_data["last_used"] = None
        profile_data["storage_path"] = None
        # The copy shares the source's proxy, so the answer would still be true —
        # but it would read as "this profile was checked", and it never was.
        profile_data["proxy_check"] = None
        # A copy is a new account that has earned nothing yet; whoever cares
        # about it protects it themselves.
        profile_data["protected"] = False

        new_profile = Profile(**profile_data)

        # A new machine unless asked otherwise. Without this the clone would
        # report the same GPU, screen, core count and seed as its source for the
        # rest of its life: two profiles that are provably one computer.
        if regenerate_fingerprint:
            new_profile.fingerprint = await self._new_machine(new_profile.browser_settings)

        # Create the directory for the new profile
        profile_dir = Path(new_profile.get_storage_path(str(self.profiles_dir)))
        profile_dir.mkdir(parents=True, exist_ok=True)

        # Copy the source profile data if it exists
        source_path = profile_data_path(self, source_profile)
        if copy_data and source_path.exists():
            try:

                def copy_tree():
                    # Caches and Chrome's run state stay behind: the locks a
                    # crash leaves are symlinks naming the old process, and the
                    # clone's first launch rebuilds every cache anyway.
                    def disposable(directory: str, names: list[str]) -> set[str]:
                        base = Path(directory).relative_to(source_path)
                        return {n for n in names if profile_archive.is_disposable(base / n)}

                    for path in source_path.rglob("*"):
                        relative = path.relative_to(source_path)
                        if path.is_symlink() and not profile_archive.is_disposable(relative):
                            raise ValueError("Cannot clone profile data containing symbolic links")
                    shutil.copytree(source_path, profile_dir, dirs_exist_ok=True, ignore=disposable)

                await run_file_operation(copy_tree)
                logger.debug(f"Copied profile data from {source_path} to {profile_dir}")
            except BaseException:
                shutil.rmtree(profile_dir, ignore_errors=True)
                raise

        # Save the new profile
        await self.storage.save_profile(new_profile)

        # Log the clone
        await self.storage.log_usage(
            UsageStats(
                profile_id=new_profile.id,
                action="clone_profile",
                details={"source_id": source_id, "new_name": new_name, "copy_data": copy_data},
            )
        )

        logger.info(f"Profile cloned: {new_profile.id}")
        return new_profile

    async def rotate_profile_fingerprint(self, profile_id: str) -> Profile | None:
        """Give a profile a new machine: new seed, hardware, screen and GPU."""
        logger.info(f"Regenerating the machine of profile {profile_id}")

        profile = await self.get_profile(profile_id)
        if not profile:
            return None
        if profile.protected:
            raise ProfileProtected(profile_id, "given a new fingerprint")

        before = machine.pinned_gpu(profile.fingerprint)
        # Without the browser installed there is nothing to generate with; the
        # old machine is dropped and the next launch generates a new one.
        profile.fingerprint = await self._new_machine(profile.browser_settings)
        profile.updated_at = datetime.now()

        # Version-checked for the same reason an edit is: this reads the row,
        # changes it and writes it back, so a save that lands in between would
        # otherwise be reverted without a word.
        await self.storage.update_profile(profile, expected_row_version=profile.row_version)

        await self.storage.log_usage(
            UsageStats(
                profile_id=profile_id,
                action="rotate_fingerprint",
                details={"from_gpu": before, "to_gpu": machine.pinned_gpu(profile.fingerprint)},
            )
        )

        logger.info(f"Machine of profile {profile_id} regenerated")
        return profile

    async def get_profile_stats(self, profile_id: str) -> dict[str, Any]:
        """Get usage statistics for a profile."""
        profile = await self.get_profile(profile_id)
        if not profile:
            return {}

        stats = await self.storage.get_profile_usage_stats(profile_id)

        # The keys here are the ones ProfileStatsResponse reads. They used to be
        # named differently — total_usage_time against total_duration_minutes,
        # and no last_session or actions at all — so the endpoint fell back to
        # its defaults and reported every profile as never used.
        return {
            "profile_id": profile_id,
            "name": profile.name,
            "status": profile.status,
            "created_at": profile.created_at,
            "last_used": profile.last_used,
            "total_sessions": len([s for s in stats if s.action == "launch_browser"]),
            # Nothing records a duration yet, so this stays 0 until something does.
            "total_duration_minutes": sum(s.duration or 0 for s in stats) // 60,
            "last_session": profile.last_used,
            "success_rate": len([s for s in stats if s.success]) / len(stats) if stats else 0,
            "actions": [
                {
                    "action": s.action,
                    "timestamp": s.timestamp.isoformat(),
                    "success": s.success,
                }
                for s in stats
            ],
        }

    async def bulk_update_profiles(self, profile_ids: list[str], updates: dict[str, Any]) -> int:
        """Update many profiles at once."""
        logger.info(f"Bulk-updating {len(profile_ids)} profiles")

        updated_count = 0
        for profile_id in profile_ids:
            try:
                result = await self.update_profile(profile_id, updates)
                if result:
                    updated_count += 1
            except Exception as e:
                logger.error(f"Failed to update profile {profile_id}: {e}")

        logger.info(f"Updated {updated_count} of {len(profile_ids)} profiles")
        return updated_count

    # Profile group operations

    async def create_group(self, name: str, description: str | None = None) -> dict[str, Any]:
        """Create a new profile group."""
        logger.info(f"Creating new group: {name}")

        group = ProfileGroup(name=name, description=description)

        await self.storage.save_profile_group(group)

        logger.info(f"Group '{name}' created with ID: {group.id}")
        return {
            "id": group.id,
            "name": group.name,
            "description": group.description,
            "created_at": group.created_at,
        }

    async def get_group(self, group_id: str) -> dict[str, Any] | None:
        """Get a group by ID."""
        groups = await self.storage.list_profile_groups()
        group = next((g for g in groups if g.id == group_id), None)
        if not group:
            return None

        # Count the profiles in the group
        profiles = await self.list_profiles(filters={"group": group_id})

        return {
            "id": group.id,
            "name": group.name,
            "description": group.description,
            "profile_count": len(profiles),
            "created_at": group.created_at,
        }

    async def list_groups(self) -> list[dict[str, Any]]:
        """List all groups."""
        groups = await self.storage.list_profile_groups()

        # Include the profile count for each group
        result = []
        for group in groups:
            profiles = await self.list_profiles(filters={"group": group.id})
            result.append(
                {
                    "id": group.id,
                    "name": group.name,
                    "description": group.description,
                    "profile_count": len(profiles),
                    "created_at": group.created_at,
                }
            )

        return result

    async def update_group(self, group_id: str, updates: dict[str, Any]) -> dict[str, Any] | None:
        """Update a group."""
        logger.info(f"Updating group {group_id}")

        groups = await self.storage.list_profile_groups()
        group = next((g for g in groups if g.id == group_id), None)
        if not group:
            return None

        # Update the group fields
        for key, value in updates.items():
            if hasattr(group, key):
                setattr(group, key, value)

        await self.storage.save_profile_group(group)

        # Count the profiles in the group
        profiles = await self.list_profiles(filters={"group": group_id})

        logger.info(f"Group {group_id} updated")
        return {
            "id": group.id,
            "name": group.name,
            "description": group.description,
            "profile_count": len(profiles),
            "created_at": group.created_at,
        }

    async def delete_group(self, group_id: str) -> bool:
        """Delete a group."""
        logger.info(f"Deleting group {group_id}")

        # Verify the group exists
        groups = await self.storage.list_profile_groups()
        group = next((g for g in groups if g.id == group_id), None)
        if not group:
            return False

        # Ungroup the profiles that belonged to this group
        profiles = await self.list_profiles(filters={"group": group_id})
        for profile in profiles:
            await self.update_profile(profile.id, {"group": None})

        # Delete the group
        success = await self.storage.delete_profile_group(group_id)

        if success:
            logger.info(f"Group {group_id} deleted")
        else:
            logger.error(f"Failed to delete group {group_id}")

        return success

    async def reconcile_os(self, profile_id: str, keep_machine: bool) -> Profile | None:
        """Put a profile's OS setting and its pinned machine back in agreement.

        The two can drift apart because a pin describes the machine forever while
        the setting is a dropdown: change it after the first launch and nothing
        objects, because the pin is what a page actually sees. There are only two
        honest ways out and they cost very different amounts.

        ``keep_machine`` puts the setting back to the OS the pin describes and
        touches nothing else — the profile is the computer it has always been.
        Otherwise the setting wins and a new machine is pinned for it: new screen,
        GPU, cores, fonts and seed. For a warmed-up account that is a real cost,
        which is why it is never the automatic answer.
        """
        profile = await self.get_profile(profile_id)
        if not profile:
            return None
        if not profile.fingerprint:
            raise ValueError("This profile has no pinned machine yet, so nothing can disagree.")

        pinned_os = machine.pinned_os(profile.fingerprint)
        if not pinned_os:
            raise ValueError("Cannot tell which operating system this profile's machine describes.")
        if pinned_os == profile.browser_settings.os:
            # Regenerating hardware is irreversible for an account, so refuse
            # rather than let a stale UI fire this at a profile that agrees.
            raise ValueError(f"This profile is already set to {pinned_os}.")

        if not keep_machine and profile.protected:
            raise ProfileProtected(profile_id, "given a new machine")

        was = profile.browser_settings.os
        if keep_machine:
            profile.browser_settings.os = pinned_os
        else:
            if not browser_install.installed_browser()[0]:
                raise ValueError(
                    "Generating a machine needs the browser. Install it in Settings first."
                )
            profile.fingerprint = await machine.generate(profile.browser_settings)

        profile.updated_at = datetime.now()
        # Version-checked for the same reason an edit is: this reads the row,
        # changes it and writes it back, so a save that lands in between would
        # otherwise be reverted without a word.
        await self.storage.update_profile(profile, expected_row_version=profile.row_version)
        await self.storage.log_usage(
            UsageStats(
                profile_id=profile_id,
                action="reconcile_os",
                details={
                    "kept": "machine" if keep_machine else "setting",
                    "from": was,
                    "to": profile.browser_settings.os,
                },
            )
        )
        logger.info(
            f"Profile {profile_id} reconciled to {profile.browser_settings.os} "
            f"by keeping the {'machine' if keep_machine else 'setting'}"
        )
        return profile

    async def clear_geography(self, profile_ids: list[str]) -> dict[str, list[str]]:
        """Drop the stated timezone so the proxy's exit address supplies it again.

        The next launch moves each machine to its exit: zone, position, WebRTC
        address and (unless they are stated too) languages. The hardware stays.
        """
        cleared: list[str] = []
        unchanged: list[str] = []
        not_found: list[str] = []

        for profile_id in profile_ids:
            profile = await self.get_profile(profile_id)
            if not profile:
                not_found.append(profile_id)
                continue
            if not profile.browser_settings.clear_geography():
                unchanged.append(profile_id)
                continue
            if profile.fingerprint:
                profile.fingerprint = machine.forget_placement(profile.fingerprint)
            profile.updated_at = datetime.now()
            # Version-checked for the same reason an edit is: this reads the
            # row, changes it and writes it back, so a save that lands in
            # between would otherwise be reverted without a word.
            await self.storage.update_profile(profile, expected_row_version=profile.row_version)
            await self.storage.log_usage(
                UsageStats(profile_id=profile_id, action="clear_geography", details={})
            )
            cleared.append(profile_id)

        logger.info(f"Cleared the geography of {len(cleared)} of {len(profile_ids)} profiles")
        return {"cleared": cleared, "unchanged": unchanged, "not_found": not_found}

    # -- Portability --------------------------------------------------------

    async def export_profile(self, profile_id: str, destination: Path) -> Path:
        """Archive a closed profile while holding its cross-instance lease."""
        async with profile_operation(self, profile_id) as profile:
            await run_file_operation(
                profile_archive.export_profile,
                profile,
                profile_data_path(self, profile),
                destination,
            )
        await self.storage.log_usage(
            UsageStats(profile_id=profile_id, action="export_profile", details={})
        )
        return destination

    async def import_profile(self, source: Path, name: str | None = None) -> Profile:
        """Create a profile from an archive, with a new id of its own."""
        record = profile_archive.read_archive(source)

        profile = Profile(**{**record, "name": name or record.get("name") or "Imported profile"})
        # A fresh id and storage path: the archive may well be restored next to
        # the profile it came from.
        profile.id = generate_profile_id()
        profile.storage_path = None
        profile.deleted_at = None
        profile.row_version = 0
        # The group id belongs to the source instance and would dangle here; the
        # user can reassign the profile to a local group.
        profile.group = None
        # The archive may carry a proxy check made on another machine, on another
        # network, at some point in the past. Whatever it said, it did not say it
        # here — same reasoning as a clone, and stronger.
        profile.proxy_check = None
        data_dir = Path(profile.get_storage_path(str(self.profiles_dir)))

        # Extraction writes into the directory before it can fail, and a profile
        # is only real once it is in the database. Without this, every refused
        # import (a bomb, a bad zip, an I/O error) would strand a directory that
        # nothing references and nothing ever deletes.
        # Exclusive creation also prevents an improbable generated-ID collision
        # from overwriting an existing directory before the DB sees the import.
        data_dir.mkdir(parents=True, exist_ok=False)
        try:
            await run_file_operation(profile_archive.extract_data, source, data_dir)
            await self.storage.save_profile(profile)
        except BaseException:
            shutil.rmtree(data_dir, ignore_errors=True)
            raise

        await self.storage.log_usage(
            UsageStats(
                profile_id=profile.id, action="import_profile", details={"name": profile.name}
            )
        )
        logger.info(f"Imported profile '{profile.name}' as {profile.id}")
        return profile

    # -- Browser control (delegated to BrowserSessionManager) ---------------

    async def launch_browser(
        self,
        profile_id: str,
        headless: bool = False,
        window_size: str | None = None,
        mode: str | None = None,
        extra_args: list[str] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Launch a Phasmid browser for a profile.

        ``mode`` is where it draws: ``headless``, ``virtual`` (its own VNC
        display) or ``window`` (the desktop); unset, ``headless`` and
        ``PPM_VNC`` decide. Every mode can be driven over CDP.

        ``window_size`` is accepted for API compatibility and ignored: the
        window is the profile's, so that the size a page measures is the size
        the machine claims.

        The lease is taken first and is the authority on "is this profile
        already running". ``active_sessions`` below is only a fast local path:
        it cannot see the web UI running beside this CLI, or the instance on
        another machine, and two of those answering "free" at once is the whole
        problem (see core/leases.py).
        """
        if window_size:
            logger.debug(f"Ignoring window_size {window_size!r}: the profile sets the window")
        installation = browser_install.require_installation()
        acquired = await self.storage.acquire_lease(
            profile_id, self.lease_holder, get_settings().lease_ttl
        )
        if not acquired:
            # The acquire is a conditional UPDATE, so it reports the same "no
            # rows changed" for a profile that is leased and for one that does
            # not exist. get_lease tells them apart: None means no such row,
            # and that is a 404, not a conflict.
            lease = await self.storage.get_lease(profile_id)
            if lease is None or not await self.get_profile(profile_id):
                raise ValueError(f"Profile with ID {profile_id} not found")
            raise ProfileLocked(profile_id, lease[0])

        # Everything from here to a launched browser runs on that lease, and if
        # any of it raises the lease has to go back before the error escapes.
        # Otherwise the profile is locked for a full TTL with no browser running
        # anywhere — worse than having no leases at all.
        #
        # On success it is deliberately kept: the session owns it until the
        # browser closes (or the process exits, where the API lifespan calls
        # release_all_leases).
        try:
            profile = await self.get_profile(profile_id)
            if not profile:
                raise ValueError(f"Profile with ID {profile_id} not found")

            if self.browser_sessions.is_running(profile_id):
                # Our own process, so the acquire above re-took our own lease
                # and the running browser still owns it. Returning here must
                # not release: that would open a window for another instance
                # while our browser is alive.
                session = self.browser_sessions.active_sessions[profile_id]
                return {
                    "status": "already_running",
                    "profile_id": profile_id,
                    "message": "Browser is already running for this profile",
                    "process_id": session.process_id,
                    "mode": session.mode,
                    "cdp": True,
                    "downloads_dir": str(self.downloads_dir(profile_id)),
                }

            data_path = profile_data_path(self, profile)
            data_path.mkdir(parents=True, exist_ok=True)
            profile.storage_path = str(data_path)
            settings = profile.browser_settings

            if profile.proxy is not None and profile.proxy.type == ProxyType.SOCKS4:
                raise BrowserLaunchError(proxy_check.SOCKS4_UNSUPPORTED)

            # Where the browser will come out. Asked through the proxy itself,
            # so the location group is placed at the address pages will see.
            exit_ip: str | None = None
            try:
                exit_ip, _ = await proxy_check.resolve_exit_ip(
                    profile.proxy, timeout=proxy_check.LAUNCH_TIMEOUT
                )
            except ConnectionError as exc:
                if profile.proxy is not None:
                    raise BrowserLaunchError(
                        f"The proxy did not answer, so the browser was not started: {exc}"
                    ) from exc
                # No proxy and no network: the machine stays where it was.
                logger.warning(f"Could not find this computer's public address: {exc}")

            # Pin the machine on the first launch and replay it on every one after;
            # move only its location group when the exit changed.
            pin = profile.fingerprint
            moved = False
            if not pin:
                pin = await machine.generate(settings, exit_ip)
            elif machine.needs_move(pin, settings, exit_ip):
                pin = await machine.move(pin, settings, exit_ip)
                moved = True
            profile.fingerprint = pin

            # A targeted write of the two columns a launch owns, not a whole
            # Profile: writing the row back would revert anything edited while
            # the exit was being looked up, and would bump a version a
            # concurrent editor is holding. Starting a browser is not an edit.
            profile.last_used = datetime.now()
            profile.updated_at = profile.last_used
            await self.storage.set_launch_pin(profile_id, pin, profile.last_used)

            await self.storage.log_usage(
                UsageStats(
                    profile_id=profile_id,
                    action="launch_browser",
                    details={"headless": headless, "mode": mode, "moved": moved},
                )
            )

            # Downloads a person starts land in the profile's own folder; an
            # automation client re-points them over CDP (see core/downloads.py).
            downloads.point_preferences_at(data_path, self.downloads_dir(profile_id))

            plan = build_plan(
                installation,
                profile,
                pin,
                data_path,
                self.run_dir,
                headless=headless,
                extra_args=extra_args,
            )
            session = await self.browser_sessions.launch(
                profile_id, plan, on_exit=self._on_browser_exit, mode=mode
            )
        except (Exception, asyncio.CancelledError):
            # CancelledError is a BaseException, not an Exception: an HTTP client
            # that disconnects mid-launch cancels this coroutine, and a lease must
            # never outlive a launch that produced no browser. Re-raised below, as
            # cancellation semantics require.
            #
            # Guarded on is_live because the lease is keyed by a process-wide
            # holder id: a second concurrent launch of the same profile holds the
            # very same lease, so releasing on this one's failure would unlock the
            # other one's live browser. Only a failure that leaves nothing running
            # may hand the lease back.
            if not self.browser_sessions.is_live(profile_id):
                await self._release_lease_quietly(profile_id)
            raise
        return {
            "status": "launched",
            "profile_id": profile_id,
            "message": "Browser launched successfully",
            "process_id": session.process_id,
            "mode": session.mode,
            "cdp": True,
            "downloads_dir": str(self.downloads_dir(profile_id)),
            "launch": {
                "process_id": session.process_id,
                "argv": [arg for arg in plan.argv if not arg.startswith("@")],
                "exit_ip": exit_ip,
                "moved": moved,
            },
        }

    async def _on_browser_exit(self, profile_id: str) -> None:
        """Record usage when a browser exits on its own (e.g. the user closes it)."""
        try:
            await self.storage.log_usage(
                UsageStats(profile_id=profile_id, action="close_browser", details={"forced": False})
            )
        except Exception as exc:  # noqa: BLE001 - logging must never break teardown
            logger.warning(f"Failed to log browser exit for {profile_id}: {exc}")

    async def close_browser(self, profile_id: str) -> dict[str, Any]:
        """Close the browser running for a profile, and hand its lease back."""
        closed = await self.browser_sessions.close_and_release(profile_id)
        if not closed:
            return {
                "status": "not_running",
                "profile_id": profile_id,
                "message": "Browser is not running for this profile",
            }
        await self.storage.log_usage(
            UsageStats(profile_id=profile_id, action="close_browser", details={"forced": True})
        )
        return {
            "status": "closed",
            "profile_id": profile_id,
            "message": "Browser closed successfully",
        }

    async def get_active_browsers(self) -> list[dict[str, Any]]:
        """Return summaries of the browsers currently running."""
        return self.browser_sessions.list_active()

    async def close_all_browsers(self) -> dict[str, Any]:
        """Close every active browser."""
        count = await self.browser_sessions.close_all()
        return {
            "status": "completed",
            "closed_count": count,
            "errors": [],
            "message": f"Closed {count} browsers",
        }

    async def _release_lease_quietly(self, profile_id: str) -> None:
        """Release a lease without letting the release's own failure surface.

        Called on paths that are already handling an error, where a second
        exception would replace the first and hide what actually went wrong.
        """
        try:
            await self.storage.release_lease(profile_id, self.lease_holder)
        except Exception as exc:  # noqa: BLE001 - never mask the original failure
            logger.warning(f"Failed to release the lease on {profile_id}: {exc}")

    async def release_all_leases(self) -> None:
        """Hand back every lease this process holds — the exit counterpart to launch.

        Without it a clean shutdown leaves leases standing for their full TTL,
        locking this instance's profiles against the whole fleet, including
        against itself after a restart. Guarded on the holder id, so another
        instance's lease is never touched, and it never raises: one stuck
        profile must not keep the rest locked.
        """
        try:
            holders = await self.storage.get_lease_holders()
        except Exception as exc:  # noqa: BLE001 - shutdown must not raise
            logger.warning(f"Could not enumerate leases to release on shutdown: {exc}")
            return
        for entry in holders:
            if entry.get("locked_by") != self.lease_holder:
                continue
            # A browser still up owns its lease: releasing it here would let
            # another instance take an identity this process is still driving.
            if self.browser_sessions.is_live(entry["id"]):
                continue
            await self._release_lease_quietly(entry["id"])

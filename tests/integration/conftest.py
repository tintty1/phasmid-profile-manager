"""Fixtures for API integration tests."""

import httpx
import pytest
from httpx import ASGITransport

from phasmid_profile_manager.api import dependencies
from phasmid_profile_manager.core.database import StorageManager
from phasmid_profile_manager.core.profile_manager import ProfileManager
from phasmid_profile_manager.core.scheduler import TaskScheduler
from phasmid_profile_manager.main import app


@pytest.fixture
async def client(tmp_path, phasmid):
    """An HTTP client wired to the app with a throwaway database."""
    storage = StorageManager(str(tmp_path / "api.db"))
    await storage.initialize()
    manager = ProfileManager(storage, str(tmp_path))
    await manager.initialize()

    dependencies.set_storage_manager(storage)
    dependencies.set_profile_manager(manager)
    from phasmid_profile_manager.api.routes.recovery import set_backup_manager
    from phasmid_profile_manager.core.backups import ProfileBackupManager

    set_backup_manager(ProfileBackupManager(manager))
    # Not started: the routes only need it to plan and to run-now; the loop
    # would poll on a real clock, which tests must not depend on.
    dependencies.set_scheduler(TaskScheduler(storage, manager))

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client

    await manager.browser_sessions.close_all()
    await storage.close()
    set_backup_manager(None)

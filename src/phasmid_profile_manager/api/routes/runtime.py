"""First-run browser download and application update preparation."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from phasmid_profile_manager.api.models.system import ApiResponse
from phasmid_profile_manager.core.browser_install import BrowserStatus, browser_installer
from phasmid_profile_manager.core.updates import UpdateStatus, check_update

router = APIRouter()


@router.get("/system/browser", response_model=ApiResponse[BrowserStatus])
async def browser_status():
    return ApiResponse(data=browser_installer.status())


@router.post("/system/browser/install", response_model=ApiResponse[BrowserStatus])
async def install_browser():
    return ApiResponse(data=browser_installer.start())


@router.get("/system/updates", response_model=ApiResponse[UpdateStatus])
async def updates():
    return ApiResponse(data=await check_update())


class UpdatePreparation(BaseModel):
    release_url: str
    backup_count: int
    message: str


@router.post("/system/updates/prepare", response_model=ApiResponse[UpdatePreparation])
async def prepare_update():
    from phasmid_profile_manager.api.routes.recovery import get_backup_manager

    update = await check_update()
    if update.error:
        raise HTTPException(503, update.error)
    if not update.available:
        raise HTTPException(409, "You are already on the latest available release.")
    result = await get_backup_manager().create_backups(reason="before_update")
    if result["skipped"]:
        failures = "; ".join(
            f"{item['profile_id']}: {item['reason']}" for item in result["skipped"]
        )
        raise HTTPException(
            409, f"Backup incomplete. Resolve these problems before updating: {failures}"
        )
    return ApiResponse(
        data=UpdatePreparation(
            release_url=update.release_url,
            backup_count=len(result["backups"]),
            message="Profiles backed up. Close the application before installing the update.",
        )
    )

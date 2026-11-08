"""API models for profile endpoints."""

from dataclasses import asdict
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from phasmid_pm.core.models import Profile, ProfileStatus, ProxyCheckRecord
from phasmid_pm.core.proxy_check import Level, ProxyCheckResult


class ProfileCreateRequest(BaseModel):
    """Request body for creating a profile."""

    name: str = Field(..., min_length=1, max_length=255, description="Profile name")
    group: str | None = Field(None, description="Profile group ID")
    browser_settings: dict[str, Any] | None = Field(
        None,
        description=(
            "Machine settings: os (linux or windows), gpu (<vendor_id>:<device_id> from "
            "GET /api/v1/fingerprints/gpus), languages and timezone (unset: from the "
            "proxy's exit address), webrtc_policy"
        ),
    )
    proxy_config: dict[str, Any] | None = Field(None, description="Proxy configuration")
    notes: str | None = Field(None, max_length=1000, description="Notes")
    generate_fingerprint: bool = Field(
        True,
        description=(
            "Generate the machine now (when the browser is installed). Otherwise the "
            "first launch generates it."
        ),
    )
    protected: bool = Field(
        False,
        description="Refuse deleting this profile or replacing its machine until unprotected",
    )

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "name": "Facebook Profile 1",
                "group": "social_media_group_id",
                "browser_settings": {"os": "linux", "gpu": "0x10de:0x2504"},
                "proxy_config": {
                    "type": "http",
                    "server": "proxy.example.com:8080",
                    "username": "user",
                    "password": "pass",
                },
                "notes": "Profile for Facebook",
                "generate_fingerprint": True,
            }
        }
    )


class ProfileUpdateRequest(BaseModel):
    """Request body for updating a profile."""

    name: str | None = Field(None, min_length=1, max_length=255)
    group: str | None = Field(None)
    status: ProfileStatus | None = Field(None)
    protected: bool | None = Field(
        None, description="Protect from deletion and machine replacement, or lift that"
    )
    browser_settings: dict[str, Any] | None = Field(None)
    proxy_config: dict[str, Any] | None = Field(None)
    notes: str | None = Field(None, max_length=1000)
    row_version: int | None = Field(
        None,
        description=(
            "The row_version this edit was based on, as returned when the profile "
            "was read. Sent back here, a save that another client landed in the "
            "meantime is refused with 409 instead of overwriting it. Omitted, the "
            "server guards only against two requests overlapping, which cannot "
            "catch an edit made against a form loaded minutes ago."
        ),
    )

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "status": "inactive",
                "notes": "Updated notes",
                "browser_settings": {"timezone": "Europe/Berlin"},
            }
        }
    )


class ProfileResponse(BaseModel):
    """Profile data returned by the API."""

    id: str
    name: str
    group: str | None
    status: ProfileStatus
    browser_settings: dict[str, Any]
    proxy_config: dict[str, Any] | None
    storage_path: str | None
    notes: str | None
    created_at: datetime
    updated_at: datetime
    last_used: datetime | None
    deleted_at: datetime | None = None
    protected: bool = Field(
        False, description="Deleting it or replacing its machine is refused until unprotected"
    )
    # A short description of the pinned machine, not the stored profile itself:
    # that is hundreds of values and tens of kilobytes, which nothing here needs.
    # GET /profiles/{id}/machine returns the whole Phasmid profile.
    fingerprint: dict[str, Any] | None = Field(
        None,
        description=(
            "Summary of the pinned machine, null until it is generated. Its values "
            "come from the Phasmid profile and may gain keys as Phasmid's schema "
            "grows; not frozen by the API contract."
        ),
    )
    proxy_check: ProxyCheckRecord | None = Field(
        None,
        description=(
            "The last answer this profile's proxy gave, or null if it has not been "
            "checked since the proxy was last changed. Only a check writes it."
        ),
    )
    row_version: int = Field(
        0,
        description=(
            "Bumped by every save. Send it back in a later update to have a "
            "concurrent save refused rather than silently overwritten."
        ),
    )

    model_config = ConfigDict(from_attributes=True)

    @classmethod
    def from_profile(cls, profile: "Profile") -> "ProfileResponse":
        """Build the API view of a profile."""
        from phasmid_pm.core import machine

        return cls(
            id=profile.id,
            name=profile.name,
            group=profile.group,
            status=profile.status,
            browser_settings=(
                profile.browser_settings.model_dump() if profile.browser_settings else {}
            ),
            proxy_config=profile.proxy.model_dump() if profile.proxy else None,
            storage_path=profile.storage_path,
            notes=profile.notes,
            created_at=profile.created_at,
            updated_at=profile.updated_at,
            last_used=profile.last_used,
            deleted_at=profile.deleted_at,
            protected=profile.protected,
            fingerprint=machine.summarize(
                profile.fingerprint,
                profile.browser_settings.os if profile.browser_settings else None,
            ),
            proxy_check=profile.proxy_check,
            row_version=profile.row_version,
        )


class ProfileListResponse(BaseModel):
    """Paginated list of profiles."""

    profiles: list[ProfileResponse]
    total: int
    page: int
    per_page: int
    has_next: bool
    has_prev: bool


class ProfileStatsResponse(BaseModel):
    """Usage statistics for a profile."""

    profile_id: str
    total_sessions: int
    total_duration_minutes: int
    last_session: datetime | None
    success_rate: float
    actions: list[dict[str, Any]] = Field(
        ..., description="Recent usage log entries; shape not covered by the API contract"
    )


class ProfileCloneRequest(BaseModel):
    """Request body for cloning a profile."""

    new_name: str = Field(..., min_length=1, max_length=255)
    regenerate_fingerprint: bool = Field(True, description="Generate a new fingerprint")
    copy_data: bool = Field(
        True,
        description=(
            "Copy the browser data (cookies, logins, storage, history). False: the "
            "clone starts as a fresh browser with the source's settings and proxy."
        ),
    )

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "new_name": "Facebook Profile 1 (copy)",
                "regenerate_fingerprint": True,
                "copy_data": True,
            }
        }
    )


class ProfileLaunchRequest(BaseModel):
    """Request body for launching a browser with a profile."""

    headless: bool = Field(False, description="Launch in headless mode")
    mode: Literal["headless", "virtual", "window"] | None = Field(
        None,
        description=(
            "Where the browser draws: headless; virtual (its own VNC display, viewable "
            "in the web UI); or window (this machine's desktop). Unset: headless "
            "decides, and a headed launch uses virtual when PPM_VNC is on."
        ),
    )
    window_size: str | None = Field(
        None,
        description="Ignored: the window is the profile's own size",
        deprecated=True,
    )
    args: list[str] | None = Field(
        None,
        description=(
            "Extra Chrome switches, appended to the command line, e.g. "
            '["--lang=de"]. Anything a page can observe should come from the profile.'
        ),
    )

    model_config = ConfigDict(json_schema_extra={"example": {"mode": "virtual"}})


class ProfileLaunchResponse(BaseModel):
    """Response returned when a browser is launched."""

    profile_id: str
    status: str = Field(..., description="launched or already_running")
    message: str
    process_id: int | None = Field(None, description="OS process id of the browser, if known")
    mode: str | None = Field(None, description="headless, virtual or window")
    cdp_endpoint: str | None = Field(
        None,
        description=(
            "CDP WebSocket of the browser: chromium.connect_over_cdp(cdp_endpoint) in "
            "Playwright, puppeteer.connect({browserWSEndpoint}). Send the API key as an "
            "x-api-key header when one is set."
        ),
    )
    downloads_dir: str | None = Field(
        None,
        description=(
            "Where the browser saves downloads, on the server. Downloads a client starts "
            "through cdp_endpoint land there too (the relay redirects them); fetch them "
            "from GET /profiles/{id}/downloads, not with Playwright's download.save_as()."
        ),
    )
    launch: dict[str, Any] = Field(
        default_factory=dict,
        description="Launch internals (command line, exit address); not covered by the API contract",
    )


class BrowserCloseResponse(BaseModel):
    """Response returned when a browser is asked to close."""

    status: str = Field(..., description="closed or not_running")
    profile_id: str
    message: str


class BrowserModeInfo(BaseModel):
    """One display mode and whether this instance can launch in it."""

    mode: Literal["headless", "virtual", "window"]
    available: bool
    reason: str | None = Field(None, description="Why it is unavailable")


class BrowserModesResponse(BaseModel):
    """The display modes a launch can ask for."""

    default: Literal["headless", "virtual", "window"] = Field(
        ..., description="What a launch without a mode uses"
    )
    modes: list[BrowserModeInfo]


class ActiveBrowserInfo(BaseModel):
    """One running browser."""

    profile_id: str
    process_id: int | None
    started_at: datetime
    vnc: bool = Field(False, description="Viewable in the web UI over /profiles/{id}/vnc")
    mode: str | None = Field(None, description="headless, virtual or window")
    cdp: bool = Field(False, description="Drivable over CDP at /profiles/{id}/cdp")


class ActiveBrowsersResponse(BaseModel):
    """The browsers currently running."""

    active_browsers: list[ActiveBrowserInfo]
    count: int


class BrowsersCloseAllResponse(BaseModel):
    """Response returned when every browser is asked to close."""

    status: str
    closed_count: int
    errors: list[str]
    message: str


class ProxyCheckRequest(BaseModel):
    """Request body for checking a proxy that is not saved yet."""

    proxy_config: dict[str, Any] | None = Field(
        None, description="Proxy to check. Omit to check the direct connection."
    )
    browser_settings: dict[str, Any] | None = Field(
        None, description="Settings to compare against the exit address"
    )

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "proxy_config": {"type": "http", "server": "proxy.example.com:8080"},
                "browser_settings": {"timezone": "Europe/Berlin"},
            }
        }
    )


class ReconcileOsRequest(BaseModel):
    """Which of the two things a profile says about its OS should win."""

    keep_machine: bool = Field(
        ...,
        description=(
            "True: put the OS setting back to what the pinned machine reports, changing "
            "no fingerprint. False: pin a new machine for the OS setting instead — new "
            "screen, GPU, cores, fonts and noise seeds."
        ),
    )

    model_config = ConfigDict(json_schema_extra={"example": {"keep_machine": True}})


class ClearGeographyRequest(BaseModel):
    """Profiles whose stored timezone and coordinates should follow the proxy again."""

    # min_length on the items too: a client building ids from empty form state
    # otherwise gets a cheerful 200 reporting "" as not_found.
    profile_ids: list[Annotated[str, StringConstraints(min_length=1)]] = Field(
        ..., min_length=1, description="Profile IDs to clear"
    )

    model_config = ConfigDict(json_schema_extra={"example": {"profile_ids": ["a1b2c3d4"]}})


class ClearGeographyResponse(BaseModel):
    """What the request did to each profile it named."""

    cleared: list[str] = Field(..., description="Had a timezone or coordinates; both are now unset")
    unchanged: list[str] = Field(..., description="Already followed the proxy")
    not_found: list[str] = Field(..., description="No such profile")


class ProxyLocationResponse(BaseModel):
    """Where a proxy actually comes out."""

    ip: str
    country: str | None = None
    timezone: str | None = None
    latitude: float | None = None
    longitude: float | None = None


class ProxyFindingResponse(BaseModel):
    """One thing worth telling the user about this proxy and this profile."""

    level: Level = Field(..., description="error, warning or info")
    field: str = Field(..., description="The setting the finding is about")
    message: str


class ProxyCheckResponse(BaseModel):
    """The result of reaching the internet through a proxy and comparing it."""

    reachable: bool
    error: str | None = None
    latency_ms: int | None = None
    location: ProxyLocationResponse | None = None
    findings: list[ProxyFindingResponse] = Field(default_factory=list)
    checked_at: datetime | None = Field(
        None,
        description=(
            "When this answer was recorded on the profile. Null for a check of a "
            "proxy that is not saved yet, which is recorded nowhere."
        ),
    )

    @classmethod
    def from_result(
        cls, result: ProxyCheckResult, checked_at: datetime | None = None
    ) -> "ProxyCheckResponse":
        return cls(
            checked_at=checked_at,
            reachable=result.reachable,
            error=result.error,
            latency_ms=result.latency_ms,
            location=(
                ProxyLocationResponse(**asdict(result.location)) if result.location else None
            ),
            findings=[ProxyFindingResponse(**asdict(f)) for f in result.findings],
        )


class DownloadInfo(BaseModel):
    """One file in a profile's downloads folder."""

    name: str
    size: int = Field(..., description="Bytes on disk so far")
    modified: datetime
    complete: bool = Field(..., description="False while the browser is still writing it")
    guid: str | None = Field(
        None,
        description=(
            "The browser's id of this download, when a CDP client started it through "
            "the relay. Playwright: the file name of download.path()."
        ),
    )


class DownloadListResponse(BaseModel):
    """A profile's downloads, newest first."""

    profile_id: str
    downloads_dir: str
    files: list[DownloadInfo]

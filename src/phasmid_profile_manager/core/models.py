"""Data models for the Phasmid profile manager."""

import re
import secrets
import string
from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Characters used for short IDs, excluding visually confusing ones (0, o, 1, l, i).
_ID_ALPHABET = "".join(c for c in (string.ascii_lowercase + string.digits) if c not in "0o1li")


def generate_short_id(length: int = 8) -> str:
    """Generate a short, readable ID.

    Every character is random, drawn from an alphabet without visually confusing
    characters (0/o, 1/l/i), which gives 31**8 ≈ 8.5e11 possible IDs at the
    default length.

    This used to derive most of the ID from a microsecond timestamp and leave
    only two random characters. IDs minted in the same microsecond — which is
    what a bulk Excel import does — then had just 961 possible values, and
    because profiles are stored with INSERT OR REPLACE keyed on the ID, a
    collision silently overwrote an existing profile.
    """
    return "".join(secrets.choice(_ID_ALPHABET) for _ in range(length))


def generate_profile_id() -> str:
    """Generate a short ID for a profile."""
    return generate_short_id(8)


def generate_group_id() -> str:
    """Generate a short ID for a group."""
    return generate_short_id(8)


def generate_schedule_id() -> str:
    """Generate a short ID for a schedule."""
    return generate_short_id(8)


class ProfileStatus(str, Enum):
    """Profile status."""

    ACTIVE = "active"
    INACTIVE = "inactive"
    BLOCKED = "blocked"
    MAINTENANCE = "maintenance"


class ProxyType(str, Enum):
    """Proxy protocol."""

    HTTP = "http"
    HTTPS = "https"
    SOCKS4 = "socks4"
    SOCKS5 = "socks5"


WebRTCPolicy = Literal[
    "default",
    "default_public_interface_only",
    "default_public_and_private_interfaces",
    "disable_non_proxied_udp",
]

OsFamily = Literal["linux", "windows"]

_GPU_KEY = re.compile(r"^0x[0-9a-f]{4}:0x[0-9a-f]{4}$")


class ProxyConfig(BaseModel):
    """Proxy configuration."""

    type: ProxyType
    server: str
    username: str | None = None
    password: str | None = None
    country: str | None = None


class BrowserSettings(BaseModel):
    """What a person chooses about a profile's machine; phasmid-profile-gen does the rest.

    The machine itself (``Profile.fingerprint``) is a full Phasmid profile,
    generated from these settings once and replayed on every launch. ``os`` and
    ``gpu`` pick the hardware and only take effect when a machine is generated.
    ``languages`` and ``timezone`` are the location group: unset, both follow
    the proxy's exit address, which is the right answer for nearly every
    profile; set, they win over it on the next launch.
    """

    os: OsFamily = "linux"
    # "<vendor_id>:<device_id>" of a GPU a captured host backs, e.g. 0x10de:0x2504;
    # GET /api/v1/fingerprints/gpus lists them. Unset: drawn from the captures.
    gpu: str | None = None
    languages: list[str] | None = None
    timezone: str | None = None
    # Chrome's WebRTC IP handling policy. "default" is what stock Chrome does;
    # mDNS already hides host candidates there.
    webrtc_policy: WebRTCPolicy = "default"

    @field_validator("gpu")
    @classmethod
    def _gpu_key(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return None
        value = value.strip().lower()
        if not _GPU_KEY.match(value):
            raise ValueError("gpu is <vendor_id>:<device_id>, e.g. 0x10de:0x2504")
        return value

    @field_validator("languages")
    @classmethod
    def _languages(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        cleaned = [lang.strip() for lang in value if lang and lang.strip()]
        return cleaned or None

    @field_validator("timezone")
    @classmethod
    def _timezone(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        value = value.strip()
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"Unknown timezone {value!r}") from exc
        return value

    def has_geography(self) -> bool:
        """Whether this profile states where it is instead of following its proxy."""
        return bool(self.timezone)

    def clear_geography(self) -> bool:
        """Drop the stated timezone so the proxy's exit address supplies it again.

        Languages are left alone: an English browser is unremarkable from any
        country, so clearing them would change the profile's identity rather
        than free it to follow the proxy.
        """
        if not self.has_geography():
            return False
        self.timezone = None
        return True


# What a finding means, defined here because both the live check and the record
# kept on a profile carry one — two definitions would be two types for the same
# value in the same API response.
Level = Literal["error", "warning", "info"]


class ProxyCheckFinding(BaseModel):
    """One thing worth telling the user about this proxy and this profile."""

    level: Level
    field: str
    message: str


class ProxyCheckRecord(BaseModel):
    """The last answer a profile's proxy gave, kept so the list can show it.

    Deliberately smaller than the live check: no coordinates, because the list
    shows where the proxy comes out, not where on the map. Whether the dot is
    green, amber or red is derived from `reachable` and the findings rather than
    stored, so an old row cannot disagree with the rules that read it.
    """

    checked_at: datetime
    reachable: bool
    error: str | None = None
    latency_ms: int | None = None
    ip: str | None = None
    country: str | None = None
    timezone: str | None = None
    findings: list[ProxyCheckFinding] = Field(default_factory=list)


class Profile(BaseModel):
    """Browser profile."""

    model_config = ConfigDict(use_enum_values=True)

    id: str = Field(default_factory=generate_profile_id)
    name: str
    group: str | None = None
    status: ProfileStatus = ProfileStatus.ACTIVE
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)
    last_used: datetime | None = None
    deleted_at: datetime | None = None

    browser_settings: BrowserSettings = Field(default_factory=BrowserSettings)
    proxy: ProxyConfig | None = None
    extensions: list[str] = Field(default_factory=list)
    storage_path: str | None = None
    notes: str | None = None
    # Guards a profile that matters (a warmed-up account) against being deleted
    # or given new hardware by mistake: those are refused until it is
    # deliberately unprotected. Edits and launches are unaffected.
    protected: bool = False

    # The machine this profile is: a full Phasmid profile JSON, generated once
    # and replayed on every launch so the profile does not look like new
    # hardware every session. See core/machine.py for what follows the proxy.
    fingerprint: dict[str, Any] | None = None

    # The last time this profile's proxy was checked, so the list can show it
    # without checking again. Cleared when the proxy changes: an answer from the
    # old proxy says nothing about the new one.
    proxy_check: ProxyCheckRecord | None = None

    # Optimistic-concurrency counter, bumped by every version-checked save. A
    # caller reads a profile, edits it, and writes back against the version it
    # read; a save that landed in between makes the write fail loudly instead of
    # reverting it. Storage bookkeeping rather than profile data, but the API
    # exposes it so a client editing a form can pass back the version it loaded.
    row_version: int = 0

    def get_storage_path(self, base_path: str = "data/profiles") -> str:
        """Return (and lazily assign) the on-disk path for this profile's data."""
        if not self.storage_path:
            self.storage_path = f"{base_path}/profile_{self.id}"
        return self.storage_path


class ProfileGroup(BaseModel):
    """A group of profiles."""

    id: str = Field(default_factory=generate_group_id)
    name: str
    description: str | None = None
    created_at: datetime = Field(default_factory=datetime.now)
    profile_count: int = 0


class UsageStats(BaseModel):
    """Profile usage record."""

    id: int | None = None
    profile_id: str
    action: str
    timestamp: datetime = Field(default_factory=datetime.now)
    duration: int | None = None  # seconds
    success: bool = True
    details: dict[str, Any] | None = None


class ProxyTestResult(BaseModel):
    """Result of testing a proxy."""

    proxy_id: str
    success: bool
    response_time: int | None = None  # milliseconds
    error_message: str | None = None
    ip_address: str | None = None
    country: str | None = None
    tested_at: datetime = Field(default_factory=datetime.now)


class SystemStatus(BaseModel):
    """System status snapshot."""

    total_profiles: int
    active_profiles: int
    running_browsers: int
    total_groups: int
    system_load: float
    memory_usage: float
    disk_usage: float
    uptime_seconds: int


class ScheduleAction(str, Enum):
    """What a schedule does to its profile when it fires.

    Deliberately absent: regenerating the hardware fingerprint. The pinned
    machine exists so a profile stays the same computer between sessions;
    swapping its GPU, screen and cores on a timer would hand a warmed-up
    account new hardware overnight — the one thing the pin prevents. Hardware
    regeneration stays a manual action (reset-fingerprint), which warns about
    the cost. Nor is there a browser-version refresh: Phasmid reports the
    version it was built from, so a profile's browser is updated by installing
    a newer release, for every profile at once, as on a real machine.
    """

    LAUNCH = "launch"


class ScheduleKind(str, Enum):
    """How a schedule's fire times are expressed."""

    INTERVAL = "interval"  # every N minutes
    DAILY = "daily"  # at HH:MM on the chosen weekdays


class ScheduleRunOutcome(str, Enum):
    """How one firing of a schedule ended."""

    OK = "ok"
    SKIPPED = "skipped"  # e.g. the browser was already running
    ERROR = "error"
    MISSED = "missed"  # fell due while the process was down; not replayed


_TIME_OF_DAY = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class Schedule(BaseModel):
    """A recurring task against one profile.

    ``at_time`` is read on the server's own clock: this is a single-process
    tool that runs next to its browsers, so "09:00" means 09:00 where the
    process runs, with no timezone bookkeeping to get wrong. ``days`` uses
    Python's weekday numbering, 0 = Monday … 6 = Sunday; empty means every day.
    """

    model_config = ConfigDict(use_enum_values=True)

    id: str = Field(default_factory=generate_schedule_id)
    profile_id: str
    action: ScheduleAction
    kind: ScheduleKind
    interval_minutes: int | None = Field(None, ge=1)
    at_time: str | None = None
    days: list[int] | None = None
    # Launch schedules only: close the browser this long after opening it, so a
    # warming session ends by itself instead of staying open until the next run
    # finds it and skips.
    run_minutes: int | None = Field(None, ge=1)
    enabled: bool = True
    next_run_at: datetime | None = None
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)

    @model_validator(mode="after")
    def _expression_is_complete(self) -> "Schedule":
        if self.kind == ScheduleKind.INTERVAL:
            if not self.interval_minutes:
                raise ValueError("An interval schedule needs interval_minutes")
        else:
            if not self.at_time or not _TIME_OF_DAY.match(self.at_time):
                raise ValueError("A daily schedule needs at_time as HH:MM (24-hour)")
        if self.days is not None:
            if any(day < 0 or day > 6 for day in self.days):
                raise ValueError("days entries are weekdays, 0 (Monday) to 6 (Sunday)")
            self.days = sorted(set(self.days)) or None
        return self


class ScheduleRun(BaseModel):
    """The record of one firing of a schedule."""

    id: int | None = None
    schedule_id: str
    started_at: datetime = Field(default_factory=datetime.now)
    finished_at: datetime | None = None
    outcome: ScheduleRunOutcome
    message: str | None = None

    model_config = ConfigDict(use_enum_values=True)

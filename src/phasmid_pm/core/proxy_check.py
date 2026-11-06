"""Does what a profile claims match where its proxy actually comes out?

A pinned machine keeps a profile's hardware honest. Its location group —
timezone, languages, position, the WebRTC address — follows the proxy: a launch
looks up the exit address and moves the machine there (``core/machine.py``),
unless the profile states a timezone of its own. A profile that reports
``Europe/Berlin`` while its proxy exits in Tokyo contradicts itself in a way any
page can measure with two lines of JavaScript.

This module resolves where a proxy really comes out, with the same database
and rules phasmid-profile-gen uses to place a generated profile (DB-IP City
Lite, timezonefinder, CLDR), and compares that with what the profile states.
"""

from __future__ import annotations

import asyncio
import ipaddress
import ssl
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from math import asin, cos, radians, sin, sqrt
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from loguru import logger

from .models import (
    BrowserSettings,
    Level,
    ProxyCheckFinding,
    ProxyCheckRecord,
    ProxyConfig,
    ProxyType,
)

# Plain-text "what is my address" endpoints, tried in order. TLS is verified, so
# a proxy that intercepts TLS fails this check; that is worth being told about.
IP_ENDPOINTS = (
    "https://api.ipify.org",
    "https://checkip.amazonaws.com",
    "https://icanhazip.com",
)

# DB-IP places an address in a city, not on a street. Below this a difference
# is database noise rather than a contradiction; above the second, no plausible
# reading of the coordinates puts them near the exit.
NEARBY_KM = 100.0
FAR_KM = 500.0

DEFAULT_TIMEOUT = 10.0

# The launch path is not a person waiting for an answer: a tarpitting proxy
# must not hold a launch open indefinitely.
LAUNCH_TIMEOUT = 8.0


def valid_ipv4(value: str) -> bool:
    try:
        return isinstance(ipaddress.ip_address(value), ipaddress.IPv4Address)
    except ValueError:
        return False


def valid_ipv6(value: str) -> bool:
    try:
        return isinstance(ipaddress.ip_address(value), ipaddress.IPv6Address)
    except ValueError:
        return False


SOCKS4_UNSUPPORTED = (
    "SOCKS4 proxies cannot be used: the exit address a launch places the machine at "
    "cannot be looked up through one. Use SOCKS5, HTTP or HTTPS."
)


class LocationUnavailable(RuntimeError):
    """The exit address could not be placed on the map."""


@dataclass(frozen=True)
class ProxyLocation:
    """Where a proxy actually comes out, as the browser's own database sees it."""

    ip: str
    country: str | None = None
    timezone: str | None = None
    latitude: float | None = None
    longitude: float | None = None


@dataclass(frozen=True)
class Finding:
    """One thing worth telling the user about this proxy and this profile."""

    level: Level
    field: str
    message: str


@dataclass
class ProxyCheckResult:
    reachable: bool
    error: str | None = None
    latency_ms: int | None = None
    location: ProxyLocation | None = None
    findings: list[Finding] = field(default_factory=list)


def record(result: ProxyCheckResult, checked_at: datetime | None = None) -> ProxyCheckRecord:
    """The part of a check worth keeping on the profile.

    Stored so the list can show a proxy's last answer without asking again, which
    is the whole point of checking on demand rather than on a timer.
    """
    return ProxyCheckRecord(
        checked_at=checked_at or datetime.now(UTC),
        reachable=result.reachable,
        error=result.error,
        latency_ms=result.latency_ms,
        ip=result.location.ip if result.location else None,
        country=result.location.country if result.location else None,
        timezone=result.location.timezone if result.location else None,
        findings=[
            ProxyCheckFinding(level=f.level, field=f.field, message=f.message)
            for f in result.findings
        ],
    )


def proxy_url(proxy: ProxyConfig) -> str:
    """The proxy as a URL, with credentials if it has them.

    Credentials are percent-encoded. Provider-issued passwords routinely contain
    "@" or ":", and interpolating one raw makes the parser split the URL in the
    wrong place — which would silently fail the check *and* the lookup that
    keeps a launch from falling back to this computer's timezone.
    """
    credentials = ""
    if proxy.username:
        credentials = quote(proxy.username, safe="")
        if proxy.password:
            credentials += f":{quote(proxy.password, safe='')}"
        credentials += "@"
    return f"{proxy.type.value}://{credentials}{proxy.server}"


def preflight(proxy: ProxyConfig) -> list[Finding]:
    """What can be said about a proxy without touching the network."""
    findings: list[Finding] = []
    if proxy.type == ProxyType.SOCKS4:
        findings.append(Finding("error", "proxy", SOCKS4_UNSUPPORTED))
    return findings


def _utc_offset(zone: str) -> float | None:
    """Hours from UTC in this zone right now, or None if the name is unknown."""
    try:
        offset = datetime.now(UTC).astimezone(ZoneInfo(zone)).utcoffset()
    except (ZoneInfoNotFoundError, ValueError):
        return None
    return None if offset is None else offset.total_seconds() / 3600


def _distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points."""
    lat1, lon1, lat2, lon2 = (radians(v) for v in (lat1, lon1, lat2, lon2))
    a = sin((lat2 - lat1) / 2) ** 2 + cos(lat1) * cos(lat2) * sin((lon2 - lon1) / 2) ** 2
    return 2 * asin(sqrt(a)) * 6371.0


# Any fixed seed: it only places the position inside the accuracy disc, which a
# check does not report. The zone and the country do not depend on it.
_LOCATE_SEED = "0" * 32


def locate(ip: str) -> ProxyLocation:
    """Place an address the way phasmid-profile-gen places a profile's exit. Blocking.

    Raises LocationUnavailable when the generator or its database is missing or
    does not know the address, so the caller can still report a working proxy
    it cannot place.
    """
    from . import browser_install
    from .browser_install import GenprofileError

    try:
        found = browser_install.run_genprofile("locate", {"ip": ip, "seed": _LOCATE_SEED})[
            "location"
        ]
    except GenprofileError as exc:
        raise LocationUnavailable(str(exc)) from exc
    return ProxyLocation(
        ip=ip,
        country=found.get("country"),
        timezone=found.get("timezone"),
        latitude=found.get("latitude"),
        longitude=found.get("longitude"),
    )


def compare(
    settings: BrowserSettings,
    location: ProxyLocation,
    pin: dict[str, Any] | None = None,
) -> list[Finding]:
    """Everything a page could notice between this profile and this exit address.

    Pure: no network, no clock beyond the current UTC offsets, so the rules are
    testable on their own. ``pin`` is the stored machine, whose position is
    compared when it has one.
    """
    findings: list[Finding] = []
    where = location.country or location.ip

    if settings.timezone and location.timezone and settings.timezone != location.timezone:
        profile_offset = _utc_offset(settings.timezone)
        exit_offset = _utc_offset(location.timezone)
        if profile_offset is not None and profile_offset == exit_offset:
            findings.append(
                Finding(
                    "info",
                    "timezone",
                    f"This profile reports {settings.timezone} while the proxy exits in "
                    f"{where} ({location.timezone}). The clock agrees; the zone names "
                    "name different places.",
                )
            )
        else:
            findings.append(
                Finding(
                    "warning",
                    "timezone",
                    f"This profile reports {settings.timezone} but the proxy exits in "
                    f"{where} ({location.timezone}). A page can compare its clock with "
                    "the address it sees and find the contradiction.",
                )
            )
    elif not settings.timezone and location.timezone:
        findings.append(
            Finding(
                "info",
                "timezone",
                f"No timezone is set, so it follows the proxy: {location.timezone}.",
            )
        )

    stated = ((pin or {}).get("locale") or {}).get("geolocation")
    public_ip = (((pin or {}).get("network") or {}).get("webrtc") or {}).get("public_ip")
    if pin and not settings.timezone and public_ip != location.ip:
        findings.append(
            Finding(
                "info",
                "location",
                "The machine was last placed at "
                + (public_ip or "no exit address")
                + f". The next launch moves it to {location.ip}; the hardware stays.",
            )
        )
        # The stored position belongs to the old exit and is about to be
        # replaced, so measuring it against the new one would only alarm.
        stated = None

    if stated:
        lat, lon = stated.get("latitude"), stated.get("longitude")
        placed = location.latitude is not None and location.longitude is not None
        if lat is not None and lon is not None and placed:
            distance = _distance_km(lat, lon, location.latitude, location.longitude)  # type: ignore[arg-type]
            if distance > FAR_KM:
                findings.append(
                    Finding(
                        "warning",
                        "geolocation",
                        f"The coordinates are {distance:,.0f} km from where the proxy "
                        f"exits ({where}). A site granted location permission sees one "
                        "place and the address says another.",
                    )
                )
            elif distance > NEARBY_KM:
                findings.append(
                    Finding(
                        "info",
                        "geolocation",
                        f"The coordinates are {distance:,.0f} km from where the proxy "
                        f"exits ({where}) — the same region, a different city.",
                    )
                )

    return findings


async def resolve_exit_ip(
    proxy: ProxyConfig | None, timeout: float = DEFAULT_TIMEOUT
) -> tuple[str, int]:
    """Ask, through the proxy, what address the internet sees. Returns (ip, ms).

    An endpoint that answers with something other than an address is treated as a
    failure and the next one is tried. A captive portal or a
    rate-limit page can answer 200 with HTML, and that string must never reach a
    profile.
    """
    if proxy is not None and proxy.type == ProxyType.SOCKS4:
        raise ConnectionError(SOCKS4_UNSUPPORTED)
    url = proxy_url(proxy) if proxy else None
    last_error: Exception | None = None

    async with httpx.AsyncClient(proxy=url, timeout=timeout) as client:
        for endpoint in IP_ENDPOINTS:
            started = time.monotonic()
            try:
                response = await client.get(endpoint)
                response.raise_for_status()
            except Exception as exc:
                last_error = exc
                logger.debug(f"Proxy check via {endpoint} failed: {exc}")
                continue
            answer = response.text.strip()
            if not valid_ipv4(answer) and not valid_ipv6(answer):
                last_error = ValueError(f"{endpoint} answered with something else")
                logger.debug(f"Proxy check via {endpoint} returned {answer[:60]!r}")
                continue
            return answer, int((time.monotonic() - started) * 1000)

    raise ConnectionError(_readable(last_error))


def _readable(error: Exception | None) -> str:
    """Turn an httpx failure into something worth showing a user."""
    if error is None:
        return "The proxy did not answer."
    if isinstance(error, httpx.HTTPStatusError):
        if error.response.status_code == 407:
            return "The proxy rejected the credentials."
        return f"The proxy answered {error.response.status_code}."
    if isinstance(error, httpx.ProxyError):
        # httpx reports a failed CONNECT as a ProxyError, not as a status, so the
        # common case of wrong credentials arrives here rather than above.
        if "407" in str(error):
            return "The proxy rejected the credentials."
        return f"The proxy refused the connection: {error}"
    if isinstance(error, httpx.TimeoutException):
        return "The proxy did not answer in time."
    if (
        isinstance(error, ssl.SSLError)
        or isinstance(error, httpx.ConnectError)
        and ("SSL" in str(error) or "CERTIFICATE" in str(error))
    ):
        return f"The proxy intercepts TLS, so its certificate could not be verified: {error}"
    if isinstance(error, httpx.UnsupportedProtocol):
        return "This proxy protocol cannot be checked; SOCKS4 is not supported."
    return f"Could not reach the internet through the proxy: {error}"


async def check(
    proxy: ProxyConfig | None,
    settings: BrowserSettings,
    timeout: float = DEFAULT_TIMEOUT,
    pin: dict[str, Any] | None = None,
) -> ProxyCheckResult:
    """Reach the internet through the proxy, place it, and compare it with the profile."""
    findings = preflight(proxy) if proxy else []

    try:
        ip, latency = await resolve_exit_ip(proxy, timeout=timeout)
    except ConnectionError as exc:
        return ProxyCheckResult(reachable=False, error=str(exc), findings=findings)

    try:
        # phasmid-profile-gen runs in a child process and reads a 127 MB database, so keep
        # it off the event loop.
        location = await asyncio.to_thread(locate, ip)
    except LocationUnavailable as exc:
        logger.warning(f"Could not place {ip}: {exc}")
        findings.append(
            Finding(
                "info",
                "proxy",
                "The proxy works, but its address could not be placed on the map. "
                "Install the browser in Settings to get the location database.",
            )
        )
        return ProxyCheckResult(
            reachable=True, latency_ms=latency, location=ProxyLocation(ip=ip), findings=findings
        )

    findings.extend(compare(settings, location, pin))
    return ProxyCheckResult(
        reachable=True, latency_ms=latency, location=location, findings=findings
    )

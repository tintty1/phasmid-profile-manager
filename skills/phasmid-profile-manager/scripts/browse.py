#!/usr/bin/env python3
"""Open a URL in a profile's browser over CDP and report what the page shows.

    browse.py PROFILE URL [--mode headless(default)|virtual|window] [--text] [--html]
                          [--screenshot out.png] [--wait-for SELECTOR] [--stop]

Launches the profile (or finds it running), connects with Playwright's
connect_over_cdp, navigates the profile's own page and prints JSON: the final
URL, the title, and the visible text or HTML when asked. Cookies and logins
persist in the profile. Without --stop the browser keeps running, so a later
call (or a person in the web UI) continues where this one left off.

Configuration is the same as ppm.py: PPM_URL, PPM_API_KEY. Needs
`pip install playwright` (no `playwright install`: the browser is the server's).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ppm import PpmError, open_profile  # noqa: E402

TEXT_LIMIT = 20_000


async def browse(args: argparse.Namespace) -> dict:
    async with open_profile(args.profile, mode=args.mode, stop_on_exit=args.stop) as (
        page,
        _context,
        info,
    ):
        response = await page.goto(args.url, wait_until="domcontentloaded", timeout=60_000)
        if args.wait_for:
            await page.wait_for_selector(args.wait_for, timeout=30_000)
        else:
            # Most pages finish their own requests within this; don't fail if not.
            try:
                await page.wait_for_load_state("networkidle", timeout=10_000)
            except Exception:  # noqa: BLE001 - a busy page is still a page
                pass
        out: dict = {
            "profile_id": info["profile_id"],
            "status": response.status if response else None,
            "url": page.url,
            "title": await page.title(),
        }
        if args.text:
            text = await page.inner_text("body")
            out["text"] = text[:TEXT_LIMIT]
            out["text_truncated"] = len(text) > TEXT_LIMIT
        if args.html:
            out["html"] = await page.content()
        if args.screenshot:
            await page.screenshot(path=args.screenshot, full_page=True)
            out["screenshot"] = args.screenshot
        return out


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("profile", help="profile id or exact name")
    parser.add_argument("url")
    # Headless unless asked: an agent should not open windows on someone's desktop.
    # A profile that is already running keeps the mode it has.
    parser.add_argument("--mode", choices=["headless", "virtual", "window"], default="headless")
    parser.add_argument("--text", action="store_true", help="print the page's visible text")
    parser.add_argument("--html", action="store_true", help="print the page's HTML")
    parser.add_argument("--screenshot", help="save a full-page PNG here")
    parser.add_argument("--wait-for", help="CSS selector to wait for before reading")
    parser.add_argument("--stop", action="store_true", help="close the browser afterwards")
    args = parser.parse_args()
    try:
        out = asyncio.run(browse(args))
    except PpmError as exc:
        print(
            json.dumps({"error": {"status": exc.status, "code": exc.code, "message": exc.message}})
        )
        sys.exit(1)
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()

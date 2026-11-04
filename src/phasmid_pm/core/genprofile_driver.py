"""Run Phasmid's profile generator from a Phasmid source tree, as a subprocess.

    python genprofile_driver.py <phasmid source> <command> <json arguments>

genprofile lives in the Phasmid repository (``tools/genprofile``) and reads its
data from that tree (``profiles/data``, the consistency harness), so it is run
from the tree that matches the installed browser rather than vendored here. It
runs in a child process with this interpreter: its data loads take seconds and
hundreds of megabytes (timezone polygons, the DB-IP database), and none of that
should live in the API process between calls.

Commands, each printing one JSON object on stdout:

- ``generate`` {os, seed, gpu?, languages?, timezone? | exit_ip?} -> {"profile": {...}}
- ``locate`` {ip, seed} -> {"location": {...}}
- ``gpus`` {} -> {"gpus": [{os, gpu, renderer, weight}, ...]}
- ``geoip`` {} -> {"path": "<mmdb>"}: download the pinned DB-IP build if missing

A failure prints {"error": "..."} and exits 2. Anything else on stderr is a bug.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path
from typing import Any


def _generate(args: dict[str, Any]) -> dict[str, Any]:
    from genprofile import geoip
    from genprofile import location as loc
    from genprofile.captures import validate
    from genprofile.generate import generate

    seed = args["seed"]
    languages = args.get("languages") or None
    location = None
    note = ""
    if args.get("exit_ip"):
        location = loc.locate(args["exit_ip"], seed, geoip.reader())
        note = f"{geoip.provenance()}, {loc.provenance()}"
    timezone = location.timezone if location else args.get("timezone")
    if not timezone:
        raise ValueError("a profile needs a timezone or an exit IP to derive one from")
    languages = languages or (location.languages if location else ["en-US", "en"])
    profile = generate(args["os"], seed, languages, timezone, args.get("gpu"), location, note)
    errors = validate(profile)
    if errors:
        raise ValueError("generated profile does not validate: " + "; ".join(errors))
    return {"profile": profile}


def _locate(args: dict[str, Any]) -> dict[str, Any]:
    from genprofile import geoip
    from genprofile import location as loc

    found = loc.locate(args["ip"], args["seed"], geoip.reader())
    return {
        "location": {
            "ip": found.public_ip,
            "country": found.country,
            "city": found.city,
            "timezone": found.timezone,
            "latitude": found.latitude,
            "longitude": found.longitude,
            "accuracy": found.accuracy,
            "languages": found.languages,
        }
    }


def _gpus(_: dict[str, Any]) -> dict[str, Any]:
    from genprofile.captures import catalog
    from genprofile.claims import claims

    seen: dict[tuple[str, str], dict[str, Any]] = {}
    for capture in catalog():
        for claim in claims(capture):
            for key in claim.gpu_keys:
                entry = seen.setdefault(
                    (capture.family, key),
                    {"os": capture.family, "gpu": key, "renderer": claim.renderer, "weight": 0},
                )
                entry["weight"] += claim.weight
    return {"gpus": sorted(seen.values(), key=lambda e: (e["os"], -e["weight"], e["gpu"]))}


def _geoip(_: dict[str, Any]) -> dict[str, Any]:
    from genprofile import geoip

    return {"path": str(geoip.fetch())}


COMMANDS = {"generate": _generate, "locate": _locate, "gpus": _gpus, "geoip": _geoip}


def main(argv: list[str]) -> int:
    source, command, raw = Path(argv[1]), argv[2], argv[3] if len(argv) > 3 else "{}"
    sys.path.insert(0, str(source / "tools" / "genprofile"))
    try:
        # genprofile prints progress of its own (the DB-IP download); keep stdout
        # for the one JSON answer.
        with contextlib.redirect_stdout(io.StringIO()) as chatter:
            result = COMMANDS[command](json.loads(raw))
        if chatter.getvalue():
            print(chatter.getvalue(), file=sys.stderr, end="")
    except Exception as exc:  # noqa: BLE001 - every failure becomes one JSON answer
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}))
        return 2
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

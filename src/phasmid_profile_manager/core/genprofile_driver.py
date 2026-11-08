"""Run phasmid-profile-gen in a child process.

    python genprofile_driver.py <command> <json arguments>

Profiles come from the ``phasmid-profile-gen`` package. It runs in a child
process with this interpreter, because placing an exit address loads the
timezone polygons and the DB-IP database, which take seconds and hundreds of
megabytes. None of that should stay in the API process between calls.

Commands, each printing one JSON object on stdout:

- ``generate`` {os, seed, gpu?, languages?, timezone? | exit_ip?} -> {"profile": {...}}
- ``locate`` {ip, seed} -> {"location": {...}}
- ``gpus`` {} -> {"gpus": [{os, gpu, renderer, weight}, ...]}: ``weight`` is how
  many captures have the GPU, so how often a draw with no GPU picks it
- ``geoip`` {} -> {"path": "<mmdb>"}: download the pinned DB-IP build if missing

A failure prints {"error": "..."} and exits 2. Anything else on stderr is a bug.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
from typing import Any


def _generate(args: dict[str, Any]) -> dict[str, Any]:
    from phasmid_profile_gen import generate_profile

    profile = generate_profile(
        args["os"],
        args["seed"],
        exit_ip=args.get("exit_ip") or None,
        timezone=None if args.get("exit_ip") else args.get("timezone"),
        languages=args.get("languages") or None,
        gpu=args.get("gpu") or None,
    )
    return {"profile": profile}


def _locate(args: dict[str, Any]) -> dict[str, Any]:
    from phasmid_profile_gen import locate

    found = locate(args["ip"], args["seed"])
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
    from phasmid_profile_gen import list_gpus

    return {
        "gpus": [
            {"os": g["os"], "gpu": g["gpu"], "renderer": g["renderer"], "weight": g["captures"]}
            for g in list_gpus()
        ]
    }


def _geoip(_: dict[str, Any]) -> dict[str, Any]:
    from phasmid_profile_gen import geoip

    return {"path": str(geoip.fetch())}


COMMANDS = {"generate": _generate, "locate": _locate, "gpus": _gpus, "geoip": _geoip}


def main(argv: list[str]) -> int:
    command, raw = argv[1], argv[2] if len(argv) > 2 else "{}"
    try:
        # Keep stdout for the one JSON answer; anything printed along the way
        # (download progress) goes to stderr.
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

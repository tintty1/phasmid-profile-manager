#!/usr/bin/env python3
"""Force-close every Phasmid browser this app started.

Emergency cleanup for browsers left running after a crash. Run:

    uv run python scripts/kill_browsers.py
"""

import sys
import time

import psutil
from loguru import logger


def _find_phasmid_processes() -> list[psutil.Process]:
    """Return every running process started with a Phasmid profile.

    Matched on --phasmid-profile, which only Phasmid browsers (and proxyroute
    in front of them) carry; a normal Chrome on this machine is left alone.
    """
    processes = []
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            if any(arg.startswith("--phasmid-profile=") for arg in proc.info["cmdline"] or []):
                processes.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return processes


def kill_phasmid_processes() -> int:
    """Terminate all Phasmid processes; force-kill any survivors."""
    processes = _find_phasmid_processes()
    if not processes:
        logger.info("No Phasmid processes found")
        return 0

    logger.info(f"Found {len(processes)} Phasmid processes")
    killed = 0
    for proc in processes:
        try:
            proc.terminate()
            killed += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    time.sleep(3)

    for proc in processes:
        try:
            if proc.is_running():
                proc.kill()
        except psutil.NoSuchProcess:
            continue

    logger.info(f"Terminated {killed} Phasmid processes")
    return killed


def main() -> None:
    try:
        killed = kill_phasmid_processes()
        print(f"Terminated {killed} processes" if killed else "No active Phasmid processes found")
    except KeyboardInterrupt:
        print("Interrupted")
        sys.exit(1)


if __name__ == "__main__":
    main()

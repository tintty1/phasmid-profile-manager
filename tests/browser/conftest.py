"""Fixtures for the tests that start a real Phasmid.

They need a local build and checkout, named the way the app takes them:

    PPM_PHASMID_BINARY=/path/to/chrome PPM_PHASMID_SOURCE=/path/to/phasmid \\
        uv run pytest -m browser --no-network

and the DB-IP database phasmid-profile-gen caches (Settings > Install browser fetches
it). Pages are served from loopback, and the exit address a launch looks up is
answered locally, so a run needs no internet.
"""

from collections.abc import Iterator

import pytest

from tests.browser.support import LocalSites, serve_local_sites


@pytest.fixture(scope="session")
def local_sites() -> Iterator[LocalSites]:
    with serve_local_sites() as sites:
        yield sites

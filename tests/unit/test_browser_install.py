"""Installing a Phasmid release, offline: GitHub is an httpx MockTransport."""

import hashlib
import io
import tarfile
import textwrap
import time
from pathlib import Path

import httpx
import pytest
import zstandard
from phasmid_profile_gen import geoip

from phasmid_pm.config import get_settings
from phasmid_pm.core import browser_install
from phasmid_pm.core.browser_install import GenprofileError, Installation
from phasmid_pm.core.browser_session import BrowserLaunchError

TAG = "155.0.8059.84-p1"
REPO = "tintty1/phasmid"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """A data directory and a geoip cache of this test's own."""
    monkeypatch.setenv("PPM_DB_PATH", str(tmp_path / "data" / "profiles.db"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    for name in (
        "PPM_PHASMID_BINARY",
        "PPM_PHASMID_SOURCE",
        "PPM_PHASMID_RELEASE",
        geoip.ENV_MMDB,
    ):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def tar_bytes(files: dict[str, bytes], top: str, mode: str = "w") -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode=mode) as archive:
        for name, data in files.items():
            info = tarfile.TarInfo(f"{top}/{name}" if not name.startswith("..") else name)
            info.size = len(data)
            info.mode = 0o755 if name == "chrome" else 0o644
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def source_files() -> dict[str, bytes]:
    return {
        "chromium_version.txt": b"155.0.8059.84\n",
        "tools/proxyroute/proxyroute.py": b"",
    }


def write_source(root: Path) -> Path:
    for name, data in source_files().items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return root


def write_geoip(tmp_path: Path) -> None:
    """The database phasmid-profile-gen pins, as if it had downloaded it."""
    path = tmp_path / "cache" / "phasmid" / "geoip" / geoip.pin().sha256 / "dbip-city-lite.mmdb"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"mmdb")


class FakeGitHub:
    """The release API, the assets and the tag archive, from memory."""

    def __init__(self, browser_files: dict[str, bytes] | None = None) -> None:
        browser = tar_bytes(browser_files or {"chrome": b"#!/bin/sh\n"}, f"phasmid-{TAG}-linux-x64")
        self.asset = zstandard.ZstdCompressor().compress(browser)
        self.checksum = hashlib.sha256(self.asset).hexdigest()
        self.source = tar_bytes(source_files(), f"phasmid-{TAG}", "w:gz")
        self.requests: list[str] = []
        self.releases = [
            {"tag_name": "156.0.0.0-p1", "draft": True, "assets": []},
            {"tag_name": "155.0.0.0-rc", "draft": False, "assets": []},
            {
                "tag_name": TAG,
                "draft": False,
                "prerelease": True,
                "assets": [
                    {"name": n, "browser_download_url": f"https://dl.example/{n}"}
                    for n in (
                        f"phasmid-{TAG}-linux-x64.tar.zst",
                        f"phasmid-{TAG}-linux-x64.tar.zst.sha256",
                    )
                ],
            },
        ]

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(url)
        if url == f"https://api.github.com/repos/{REPO}/releases":
            return httpx.Response(200, json=self.releases)
        if url.endswith(".tar.zst.sha256"):
            return httpx.Response(200, text=f"{self.checksum}  phasmid-{TAG}-linux-x64.tar.zst\n")
        if url.endswith(".tar.zst"):
            return httpx.Response(200, content=self.asset)
        if url == f"https://github.com/{REPO}/archive/refs/tags/{TAG}.tar.gz":
            return httpx.Response(200, content=self.source)
        return httpx.Response(404)


@pytest.fixture
def github(monkeypatch, tmp_path):
    fake = FakeGitHub()
    real_client = httpx.Client

    def client(*args, **kwargs):
        return real_client(transport=httpx.MockTransport(fake.handler))

    monkeypatch.setattr(browser_install.httpx, "Client", client)

    def fake_genprofile(command, args=None):
        assert command == "geoip"
        write_geoip(tmp_path)
        return {"path": "mmdb"}

    monkeypatch.setattr(browser_install, "run_genprofile", fake_genprofile)
    return fake


def wait_done(installer: browser_install.BrowserInstaller) -> browser_install.BrowserStatus:
    for _ in range(200):
        status = installer.status()
        if status.state in ("ready", "error"):
            return status
        time.sleep(0.02)
    raise AssertionError("the install never finished")


# -- readiness -----------------------------------------------------------------


def test_nothing_installed_is_not_ready_and_launching_says_what_to_do():
    assert browser_install.installed_browser() == (False, None)
    with pytest.raises(BrowserLaunchError, match="Install browser"):
        browser_install.require_installation()


def test_a_complete_download_is_ready(tmp_path):
    root = tmp_path / "data" / "phasmid"
    binary = root / TAG / "browser" / "chrome"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    write_source(root / TAG / "source")
    write_geoip(tmp_path)
    (root / "current").write_text(TAG + "\n")

    assert browser_install.installed_browser() == (True, TAG)
    assert browser_install.require_installation().binary == binary


def test_without_the_location_database_it_is_not_ready(tmp_path):
    root = tmp_path / "data" / "phasmid"
    binary = root / TAG / "browser" / "chrome"
    binary.parent.mkdir(parents=True)
    binary.write_text("")
    binary.chmod(0o755)
    write_source(root / TAG / "source")
    (root / "current").write_text(TAG)

    assert browser_install.installed_browser() == (False, TAG)


def test_a_location_database_named_in_the_environment_is_used(tmp_path, monkeypatch):
    mmdb = tmp_path / "other.mmdb"
    monkeypatch.setenv(geoip.ENV_MMDB, str(mmdb))
    assert not browser_install.geoip_ready()
    mmdb.write_bytes(b"mmdb")
    assert browser_install.geoip_ready()


def test_a_current_file_naming_a_path_is_ignored(tmp_path):
    root = tmp_path / "data" / "phasmid"
    root.mkdir(parents=True)
    (root / "current").write_text("../../etc")
    assert browser_install.current_installation() is None


def test_a_local_build_and_checkout_are_used_as_configured(tmp_path, monkeypatch):
    binary = tmp_path / "build" / "chrome"
    binary.parent.mkdir()
    binary.write_text("")
    binary.chmod(0o755)
    source = write_source(tmp_path / "checkout")
    write_geoip(tmp_path)
    monkeypatch.setenv("PPM_PHASMID_BINARY", str(binary))
    monkeypatch.setenv("PPM_PHASMID_SOURCE", str(source))
    get_settings.cache_clear()

    installed, version = browser_install.installed_browser()

    assert installed is True
    assert version == "155.0.8059.84 (local)"
    assert browser_install.current_installation() == Installation(version, binary, source)


# -- installing ----------------------------------------------------------------


def test_the_newest_release_with_a_linux_build_is_installed(tmp_path, github):
    installer = browser_install.BrowserInstaller()

    installer.start()
    status = wait_done(installer)

    assert status.state == "ready", status.error
    assert status.version == TAG
    root = tmp_path / "data" / "phasmid"
    assert (root / "current").read_text().strip() == TAG
    assert (root / TAG / "browser" / "chrome").is_file()
    assert (root / TAG / "source" / "tools" / "proxyroute" / "proxyroute.py").is_file()
    assert not list((root / TAG).glob(".download-*")), "downloads are cleaned up"


def test_a_checksum_mismatch_installs_nothing(tmp_path, github):
    github.checksum = "0" * 64
    installer = browser_install.BrowserInstaller()

    installer.start()
    status = wait_done(installer)

    assert status.state == "error"
    assert "checksum" in (status.error or "")
    assert not (tmp_path / "data" / "phasmid" / "current").exists()
    assert not (tmp_path / "data" / "phasmid" / TAG / "browser").exists()


def test_an_archive_reaching_outside_its_directory_is_refused(tmp_path, monkeypatch):
    evil = tar_bytes({"chrome": b"", "../../escaped": b"x"}, f"phasmid-{TAG}-linux-x64")
    archive = tmp_path / "evil.tar.zst"
    archive.write_bytes(zstandard.ZstdCompressor().compress(evil))

    with pytest.raises(tarfile.TarError):
        browser_install.extract_zst(archive, tmp_path / "out" / "browser")

    assert not (tmp_path / "escaped").exists()


def test_a_pinned_tag_is_asked_for_by_name():
    release = FakeGitHub().releases[-1]

    def handler(request):
        if str(request.url).endswith(f"/releases/tags/{TAG}"):
            return httpx.Response(200, json=release)
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert browser_install.find_release(client, REPO, TAG)["tag_name"] == TAG


def test_no_linux_build_is_an_error_that_says_so():
    def handler(request):
        return httpx.Response(200, json=[{"tag_name": "x", "draft": False, "assets": []}])

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RuntimeError, match="Linux x64 build"):
            browser_install.find_release(client, REPO, None)


def test_repeated_install_clicks_share_one_job(github):
    installer = browser_install.BrowserInstaller()

    installer.start()
    installer.start()
    wait_done(installer)

    assert sum(url.endswith(".tar.zst") for url in github.requests) == 1


def test_a_failed_install_can_be_retried(github):
    installer = browser_install.BrowserInstaller()
    github.checksum = "0" * 64
    installer.start()
    assert wait_done(installer).state == "error"

    github.checksum = hashlib.sha256(github.asset).hexdigest()
    installer.start()

    assert wait_done(installer).state == "ready"


# -- running genprofile --------------------------------------------------------


@pytest.fixture
def driver(tmp_path, monkeypatch):
    """A stand-in for genprofile_driver.py that answers with what it is told."""
    script = tmp_path / "driver.py"
    script.write_text(
        textwrap.dedent(
            """\
            import json, sys
            mode = json.loads(sys.argv[2]).get("mode")
            if mode == "ok":
                print(json.dumps({"profile": {"seed": "s"}}))
            elif mode == "refused":
                print(json.dumps({"error": "GenerationError: no capture"}))
                sys.exit(2)
            else:
                print("Traceback: something broke", file=sys.stderr)
                sys.exit(1)
            """
        )
    )
    monkeypatch.setattr(browser_install, "DRIVER", script)


@pytest.mark.usefixtures("driver")
def test_genprofile_answers_are_returned():
    assert browser_install.run_genprofile("generate", {"mode": "ok"}) == {"profile": {"seed": "s"}}


@pytest.mark.usefixtures("driver")
def test_a_refusal_is_raised_in_genprofiles_words():
    with pytest.raises(GenprofileError, match="no capture"):
        browser_install.run_genprofile("generate", {"mode": "refused"})


@pytest.mark.usefixtures("driver")
def test_a_crash_reports_the_end_of_stderr():
    with pytest.raises(GenprofileError, match="something broke"):
        browser_install.run_genprofile("generate", {"mode": "crash"})

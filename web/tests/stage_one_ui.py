"""Offline UI contract smoke tests for the desktop static export.

Run after `NEXT_EXPORT=1 npm run build:static`:
  .venv/bin/python web/tests/stage_one_ui.py
Requires Playwright Chromium (`playwright install chromium`).
"""
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
import json
import unittest

from playwright.sync_api import sync_playwright, expect


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


class DesktopUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1] / "out"
        if not (root / "index.html").exists():
            raise RuntimeError("Build the static UI first: NEXT_EXPORT=1 npm run build:static")
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(root)))
        cls.thread = Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch()
        cls.origin = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.context = self.browser.new_context(viewport={"width": 1280, "height": 900})
        self.page = self.context.new_page()
        self.calls = []
        self.browser_state = {"installed": False, "version": None, "state": "idle", "progress": None, "message": "Browser not installed", "error": None}
        self.profiles = []
        self.backups = []
        self.trash = []
        self.config = {"version": "0.5.0", "host": "127.0.0.1", "port": 8000, "database_path": "/local/profiles.db", "api_key_set": False, "user_auth_enabled": False, "encryption_enabled": True, "cors_origins": [], "browser_available": True, "browser_version": "146.0.1", "uptime_seconds": 60}
        self.active = []
        self.gpus = [
            {"os": "linux", "gpu": "0x10de:0x2504", "renderer": "ANGLE (NVIDIA Corporation, NVIDIA GeForce RTX 3060/PCIe/SSE2, OpenGL ES 3.2)", "weight": 3},
            {"os": "linux", "gpu": "0x8086:0x3185", "renderer": "ANGLE (Intel, Mesa Intel(R) UHD Graphics 600 (GLK 2), OpenGL ES 3.2)", "weight": 1},
        ]
        self.page.route("**/api/v1/**", self.route)

    def tearDown(self):
        self.context.close()

    def route(self, route):
        request = route.request
        path = request.url.split("/api/v1", 1)[1]
        self.calls.append((request.method, path, request.post_data))
        data = {}
        envelope = False
        if path == "/auth/session":
            data = {"user_auth_enabled": False, "authenticated": True, "username": None}
        elif path == "/system/config":
            data, envelope = self.config, True
        elif path == "/system/status":
            data = {"total_profiles": len(self.profiles), "active_profiles": 0, "running_browsers": 0, "total_groups": 0, "system_load": 0, "memory_usage": 0, "disk_usage": 0, "uptime_seconds": 60}
        elif path.startswith("/profiles?"):
            data = {"profiles": self.profiles, "total": len(self.profiles), "page": 1, "per_page": 100, "has_next": False, "has_prev": False}
        elif path == "/profiles" and request.method == "POST":
            data = {"id": "new-profile", **request.post_data_json}
        elif path.startswith("/profiles/") and request.method == "PUT":
            profile_id = path.split("/")[2]
            current = next(p for p in self.profiles if p["id"] == profile_id)
            body = request.post_data_json
            data = {**current, **{k: v for k, v in body.items() if k != "browser_settings"}, "browser_settings": {**current["browser_settings"], **body["browser_settings"]}, "row_version": current["row_version"] + 1}
        elif path.startswith("/profiles/") and path.endswith("/machine"):
            data = {"id": "pinned-machine", "os": {"family": "linux", "version": "Ubuntu 24.04"}}
        elif path == "/groups":
            data = {"groups": [], "total": 0}
        elif path == "/browsers/active":
            data = {"active_browsers": self.active, "count": len(self.active)}
        elif path == "/browsers/modes":
            data = {"default": "headless", "modes": [{"mode": "headless", "available": True, "reason": None}]}
        elif path.startswith("/fingerprints/gpus"):
            os_family = path.partition("os=")[2] or None
            gpus = [g for g in self.gpus if os_family is None or g["os"] == os_family]
            data, envelope = {"gpus": gpus, "total": len(gpus)}, True
        elif path == "/system/browser":
            data, envelope = self.browser_state, True
        elif path == "/system/browser/install":
            self.browser_state = {**self.browser_state, "state": "downloading", "progress": 35, "message": "Downloading browser"}
            data, envelope = self.browser_state, True
        elif path == "/system/updates":
            data, envelope = {"current_version": "0.5.0", "latest_version": "0.6.0", "available": True, "release_url": "https://github.com/tintty1/phasmid-profile-manager/releases/tag/v0.6.0", "assets": []}, True
        elif path == "/system/updates/prepare":
            data, envelope = {"release_url": "https://github.com/tintty1/phasmid-profile-manager/releases/tag/v0.6.0", "backup_count": 1, "message": "1 backup created. Release ready."}, True
        elif path == "/system/backups":
            data, envelope = {"backups": self.backups}, True
        elif path.startswith("/system/backups/") and path.endswith("/restore"):
            data, envelope = {"id": "restored-copy", "name": "Recovered profile"}, True
        elif path == "/trash/profiles":
            data, envelope = {"profiles": self.trash}, True
        elif path.endswith("?confirm=true"):
            self.trash = []
            data, envelope = None, True
        else:
            raise AssertionError(f"Unexpected API call: {request.method} {path}")
        route.fulfill(status=200, content_type="application/json", body=json.dumps({"success": True, "data": data} if envelope else data))

    def test_explicit_browser_install_and_simple_profile(self):
        self.page.goto(self.origin)
        expect(self.page.get_by_role("heading", name="Set up your browser")).to_be_visible()
        self.assertFalse(any(path == "/system/browser/install" for _, path, _ in self.calls))
        self.page.get_by_role("button", name="Install browser", exact=True).click()
        expect(self.page.get_by_role("progressbar")).to_have_attribute("value", "35")
        self.browser_state = {**self.browser_state, "installed": True, "state": "ready", "version": "135", "progress": 100, "message": "Ready"}
        expect(self.page.get_by_role("heading", name="Ready for your first profile")).to_be_visible()
        self.page.get_by_role("button", name="Create first profile", exact=True).click()
        expect(self.page.get_by_label("Operating system", exact=True)).to_have_value("linux")
        expect(self.page.get_by_label("GPU", exact=True)).not_to_be_visible()
        self.page.get_by_text("Advanced browser and fingerprint settings", exact=True).click()
        gpu = self.page.get_by_label("GPU", exact=True)
        expect(gpu.locator("option", has_text="NVIDIA GeForce RTX 3060")).to_have_count(1)
        gpu.select_option(label="NVIDIA GeForce RTX 3060")
        self.page.get_by_label("Languages", exact=True).fill("de-DE, de")
        self.page.get_by_label("WebRTC", exact=True).select_option("disable_non_proxied_udp")
        self.page.get_by_label("Name", exact=True).fill("First profile")
        self.page.get_by_role("button", name="Create profile", exact=True).click()
        expect(self.page.get_by_role("dialog")).not_to_be_visible()
        saved = next(json.loads(body) for method, path, body in self.calls if method == "POST" and path == "/profiles")
        self.assertEqual(saved["browser_settings"], {"os": "linux", "gpu": "0x10de:0x2504", "languages": ["de-DE", "de"], "timezone": None, "webrtc_policy": "disable_non_proxied_udp"})
        self.assertTrue(saved["generate_fingerprint"])
        self.assertNotIn("fingerprint_preset", saved)
        self.assertEqual(sum(path == "/system/browser/install" for _, path, _ in self.calls), 1)

    def test_returning_user_is_not_prompted(self):
        self.browser_state = {**self.browser_state, "installed": True, "state": "ready", "version": "135"}
        self.profiles = [{"id": "existing", "name": "Existing", "status": "active", "browser_settings": {"os": "linux"}, "created_at": "2026-01-01T00:00:00Z", "row_version": 0}]
        self.page.goto(self.origin)
        expect(self.page.get_by_role("cell", name="Existing", exact=True)).to_be_visible()
        expect(self.page.get_by_role("region", name="Browser setup")).not_to_be_visible()

    def test_edit_shows_machine_and_cdp(self):
        self.browser_state = {**self.browser_state, "installed": True, "state": "ready", "version": "146.0.1"}
        self.context.grant_permissions(["clipboard-read", "clipboard-write"], origin=self.origin)
        fingerprint = {"id": "pinned-machine", "seed": "abc", "os": "linux", "os_version": "Ubuntu 24.04", "platform": "Linux x86_64", "hardware_concurrency": 12, "device_memory": 16, "screen": "1920x1080", "device_pixel_ratio": 1, "gpu": "ANGLE (NVIDIA Corporation, NVIDIA GeForce RTX 3060/PCIe/SSE2, OpenGL ES 3.2)", "gpu_id": "0x10de:0x2504", "font_count": 214, "timezone": "Asia/Tokyo", "languages": ["ja-JP", "ja"], "public_ip": "203.0.113.7", "has_geolocation": True, "browser_version": "146.0.1", "pinned_os": "linux", "settings_os": "linux", "os_mismatch": False}
        self.profiles = [{"id": "pinned", "name": "Pinned", "status": "active", "browser_settings": {"os": "linux", "gpu": None, "languages": None, "timezone": "Asia/Tokyo", "webrtc_policy": "default"}, "fingerprint": fingerprint, "created_at": "2026-01-01T00:00:00Z", "row_version": 3}]
        self.active = [{"profile_id": "pinned", "process_id": 1, "started_at": "2026-01-01T00:00:00Z", "vnc": False, "mode": "headless", "cdp": True}]
        self.page.goto(self.origin)

        self.page.get_by_role("button", name="Actions for Pinned", exact=True).click()
        self.page.get_by_role("menuitem", name="Copy CDP URL").click()
        expect(self.page.get_by_text("CDP URL copied", exact=True)).to_be_visible()
        copied = self.page.evaluate("navigator.clipboard.readText()")
        self.assertEqual(copied, f"ws://127.0.0.1:{self.server.server_port}/api/v1/profiles/pinned/cdp")

        self.page.get_by_role("button", name="Actions for Pinned", exact=True).click()
        self.page.get_by_role("menuitem", name="Edit").click()
        form = self.page.get_by_role("dialog", name="Edit profile")
        expect(form).to_contain_text("Ubuntu 24.04")
        expect(form).to_contain_text("1920x1080 @ 1x")
        expect(form).to_contain_text("16 GB")
        expect(form).to_contain_text("Phasmid 146.0.1")
        expect(form).to_contain_text("203.0.113.7")
        expect(form).to_contain_text("OS and GPU take effect only when the machine is regenerated")

        form.get_by_role("button", name="View machine JSON").click()
        machine = self.page.get_by_role("dialog", name="Machine JSON")
        expect(machine).to_contain_text('"id": "pinned-machine"')
        self.page.keyboard.press("Escape")
        expect(machine).not_to_be_visible()
        expect(form).to_be_visible()

        form.get_by_label("GPU", exact=True).select_option("0x10de:0x2504")
        form.get_by_label("Timezone", exact=True).fill("")
        form.get_by_role("button", name="Save changes", exact=True).click()
        expect(form).not_to_be_visible()
        saved = next(json.loads(body) for method, path, body in self.calls if method == "PUT" and path == "/profiles/pinned")
        self.assertEqual(saved["browser_settings"], {"os": "linux", "gpu": "0x10de:0x2504", "languages": None, "timezone": None, "webrtc_policy": "default"})
        self.assertEqual(saved["row_version"], 3)

    def test_schedule_offers_only_launch(self):
        self.page.route("**/api/v1/schedules", lambda route: route.fulfill(status=200, content_type="application/json", body=json.dumps({"schedules": [], "total": 0})))
        self.page.goto(f"{self.origin}/schedules/")
        self.page.get_by_role("button", name="New schedule").first.click()
        task = self.page.get_by_label("Task", exact=True)
        expect(task.locator("option")).to_have_count(1)
        expect(task.locator("option")).to_have_attribute("value", "launch")

    def test_release_download_requires_backup_preparation(self):
        self.page.goto(f"{self.origin}/settings/")
        self.page.get_by_role("button", name="Check for updates", exact=True).click()
        expect(self.page.get_by_text("Version 0.6.0 is available. You have 0.5.0.")).to_be_visible()
        expect(self.page.get_by_role("link", name="Open release downloads")).not_to_be_visible()
        self.page.get_by_role("button", name="Back up and prepare update").click()
        expect(self.page.get_by_role("link", name="Open release downloads")).to_have_attribute("href", "https://github.com/tintty1/phasmid-profile-manager/releases/tag/v0.6.0")
        self.assertEqual(sum(path == "/system/updates/prepare" for _, path, _ in self.calls), 1)

    def test_backup_restore_creates_a_separate_profile(self):
        self.backups = [{"id": "saved", "profile_id": "original", "profile_name": "Original profile", "created_at": "2026-01-01T00:00:00Z", "reason": "manual", "size_bytes": 1024}]
        self.page.goto(f"{self.origin}/settings/")
        self.page.get_by_role("button", name="Restore backup of Original profile", exact=False).click()
        expect(self.page.get_by_role("dialog")).to_contain_text("Your current profiles and this backup are preserved.")
        self.page.get_by_role("button", name="Restore as new profile", exact=True).click()
        expect(self.page.get_by_text("Backup restored as a new profile", exact=True)).to_be_visible()
        self.assertTrue(any(method == "POST" and path == "/system/backups/saved/restore" for method, path, _ in self.calls))
        self.assertFalse(any(method in {"PUT", "DELETE"} for method, _, _ in self.calls))

    def test_permanent_delete_requires_name_confirmation(self):
        self.trash = [{"id": "trashed", "name": "Recoverable", "deleted_at": "2026-01-01T00:00:00Z"}]
        self.page.goto(f"{self.origin}/trash/")
        self.page.get_by_role("button", name="Permanently delete Recoverable").click()
        confirm = self.page.get_by_role("button", name="Delete permanently", exact=True).last
        expect(confirm).to_be_disabled()
        self.page.get_by_label("Type Recoverable to confirm").fill("Recoverable")
        expect(confirm).to_be_enabled()
        confirm.click()
        expect(self.page.get_by_role("heading", name="Trash is empty")).to_be_visible()
        self.assertEqual(sum(method == "DELETE" and path == "/trash/profiles/trashed?confirm=true" for method, path, _ in self.calls), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)

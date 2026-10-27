#!/usr/bin/env python3
import importlib.util
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("fleet_ghdrop", ROOT / "lib/fleet_ghdrop.py")
media = importlib.util.module_from_spec(spec)
spec.loader.exec_module(media)
NOW = datetime(2026, 10, 24, tzinfo=timezone.utc)
SERVICE = "https://files.example.workers.dev"
IMAGE = SERVICE + "/f/0123456789abcdef/screen.png"
REF = "https://github.com/owner/repo/pull/1"
OTHER = "https://github.com/owner/other/issues/2"


class CleanupTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        config = self.home / ".config/ghdrop/config.json"
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({"url": SERVICE}))
        self.path = self.home / ".local/state/ghdrop/images.json"
        self.path.parent.mkdir(parents=True)
        self.entry = {"refs": [REF], "seen_at": (NOW - timedelta(days=11)).isoformat(), "keep": False, "deleted_at": None}
        self.write()
        self.items = {REF: self.issue(REF), OTHER: self.issue(OTHER)}
        self.search = {"incomplete_results": False, "total_count": 0, "items": []}
        gh = mock.patch.object(media, "github", side_effect=self.github)
        self.gh = gh.start()
        self.addCleanup(gh.stop)
        commands = mock.patch.object(media, "command", return_value="")
        self.command = commands.start()
        self.addCleanup(commands.stop)
        output = redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)

    def issue(self, url, days=11, state="closed", labels=None):
        return {"html_url": url, "state": state, "closed_at": (NOW - timedelta(days=days)).isoformat() if state == "closed" else None, "labels": labels or []}

    def github(self, home, endpoint):
        if endpoint.startswith("search/issues?"):
            return self.search
        return next(item for url, item in self.items.items() if media.reference_endpoint(url) == endpoint)

    def write(self):
        self.path.write_text(json.dumps({"version": 1, "images": {IMAGE: self.entry}}))

    def test_closed_images_are_deleted_after_ten_days_and_not_deleted_twice(self):
        self.entry["seen_at"] = (NOW - timedelta(days=10)).isoformat()
        self.items[REF] = self.issue(REF, days=10)
        self.write()
        media.cleanup(self.home, now=NOW)
        self.command.assert_called_once_with(self.home, ["ghdrop", "delete", "--url", SERVICE, IMAGE])
        self.assertEqual(json.loads(self.path.read_text())["images"][IMAGE]["deleted_at"], NOW.isoformat())
        media.cleanup(self.home, now=NOW)
        self.command.assert_called_once()

    def test_open_recent_pinned_and_unknown_references_stay(self):
        cases = [self.issue(REF, state="open"), self.issue(REF, days=9), self.issue(REF, labels=[{"name": "KEEP-MEDIA"}])]
        for item in cases:
            with self.subTest(item=item):
                self.items[REF] = item
                media.cleanup(self.home, now=NOW)
                self.command.assert_not_called()
        self.items[REF] = self.issue(OTHER)
        with self.assertRaises(media.fleet.FleetError):
            media.cleanup(self.home, now=NOW)
        self.command.assert_not_called()

    def test_recent_upload_on_an_old_closed_pr_stays(self):
        self.entry["seen_at"] = (NOW - timedelta(days=9, hours=23)).isoformat()
        self.write()
        media.cleanup(self.home, now=NOW)
        self.gh.assert_not_called()
        self.command.assert_not_called()

    def test_explicit_keep_and_untracked_images_stay(self):
        media.keep(self.home, [IMAGE])
        media.cleanup(self.home, now=NOW)
        self.gh.assert_not_called()
        self.path.write_text('{"version":1,"images":{}}')
        media.cleanup(self.home, now=NOW)
        self.command.assert_not_called()

    def test_image_shared_with_an_open_issue_stays(self):
        self.items[OTHER] = self.issue(OTHER, state="open")
        self.search.update(total_count=1, items=[{"html_url": OTHER}])
        media.cleanup(self.home, now=NOW)
        self.command.assert_not_called()

    def test_all_tracked_references_must_be_closed(self):
        self.entry["refs"].append(OTHER)
        self.items[OTHER] = self.issue(OTHER, state="open")
        self.write()
        media.cleanup(self.home, now=NOW)
        self.command.assert_not_called()

    def test_discovered_reference_is_kept_when_repo_access_is_lost(self):
        self.items[OTHER] = self.issue(OTHER, state="open")
        self.search.update(total_count=1, items=[{"html_url": OTHER}])
        media.cleanup(self.home, now=NOW)
        self.assertIn(OTHER, json.loads(self.path.read_text())["images"][IMAGE]["refs"])
        self.search = {"incomplete_results": False, "total_count": 0, "items": []}

        def lost_access(home, endpoint):
            if endpoint == media.reference_endpoint(OTHER):
                raise media.fleet.FleetError("private repo no longer readable")
            return self.github(home, endpoint)

        self.gh.side_effect = lost_access
        with self.assertRaises(media.fleet.FleetError):
            media.cleanup(self.home, now=NOW)
        self.command.assert_not_called()

    def test_dry_run_also_remembers_discovered_references(self):
        self.items[OTHER] = self.issue(OTHER, state="open")
        self.search.update(total_count=1, items=[{"html_url": OTHER}])
        media.cleanup(self.home, dry_run=True, now=NOW)
        self.assertIn(OTHER, json.loads(self.path.read_text())["images"][IMAGE]["refs"])
        self.command.assert_not_called()

    def test_reference_is_checked_again_immediately_before_deletion(self):
        closed = self.issue(REF)
        opened = self.issue(REF, state="open")
        self.gh.side_effect = [closed, self.search, opened]
        media.cleanup(self.home, now=NOW)
        self.command.assert_not_called()

    def test_incomplete_or_truncated_search_never_deletes(self):
        for search in [{"incomplete_results": True, "total_count": 0, "items": []}, {"incomplete_results": False, "total_count": 101, "items": []}]:
            with self.subTest(search=search):
                self.search = search
                with self.assertRaises(media.fleet.FleetError):
                    media.cleanup(self.home, now=NOW)
                self.command.assert_not_called()

    def test_github_failure_never_deletes(self):
        self.gh.side_effect = media.fleet.FleetError("unavailable")
        with self.assertRaises(media.fleet.FleetError):
            media.cleanup(self.home, now=NOW)
        self.command.assert_not_called()

    def test_dry_run_does_not_delete_or_change_registry(self):
        original = self.path.read_bytes()
        media.cleanup(self.home, dry_run=True, now=NOW)
        self.command.assert_not_called()
        self.assertEqual(self.path.read_bytes(), original)

    def test_foreign_urls_non_images_and_bad_dates_fail_before_deletion(self):
        for url in ["https://other.example/f/0123456789abcdef/screen.png", IMAGE.replace(".png", ".zip")]:
            with self.subTest(url=url):
                self.path.write_text(json.dumps({"version": 1, "images": {url: self.entry}}))
                with self.assertRaises(media.fleet.FleetError):
                    media.cleanup(self.home, now=NOW)
                self.command.assert_not_called()
        self.entry["seen_at"] = "2026-10-01"
        self.write()
        with self.assertRaises(ValueError):
            media.cleanup(self.home, now=NOW)
        self.command.assert_not_called()

    def test_track_adds_shared_references_resets_wait_and_preserves_keep(self):
        self.entry["keep"] = True
        self.write()
        media.track(self.home, OTHER, [IMAGE], now=NOW)
        entry = json.loads(self.path.read_text())["images"][IMAGE]
        self.assertEqual(entry["refs"], [REF, OTHER])
        self.assertEqual(entry["seen_at"], NOW.isoformat())
        self.assertTrue(entry["keep"])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_failed_delete_does_not_mark_image_as_deleted(self):
        self.command.side_effect = media.fleet.FleetError("failed")
        with self.assertRaises(media.fleet.FleetError):
            media.cleanup(self.home, now=NOW)
        self.assertIsNone(json.loads(self.path.read_text())["images"][IMAGE]["deleted_at"])


if __name__ == "__main__":
    unittest.main(argv=[__file__])

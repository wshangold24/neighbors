"""Tests for --debug mode: HTTP history, report contents, secret redaction,
crash capture, and the CLI end-to-end debug flow."""

from __future__ import annotations

import json
import logging

from .conftest import make_session, run_cli, seed_home

from neighbors.debug import REDACTED, DebugSession
from neighbors.knn import Query, knn
from neighbors.models import Preferences
from neighbors.providers.demo import DemoProvider


# --------------------------------------------------------------------------
# SafeSession.history (the raw material of the report)
# --------------------------------------------------------------------------

class TestHttpHistory:
    def test_records_response_and_cache_hit(self, http_server, tmp_path):
        base, _ = http_server
        s = make_session(tmp_path)
        s.get(f"{base}/ok")
        s.get(f"{base}/ok")
        events = [h["event"] for h in s.history]
        assert events == ["response", "cache_hit"]
        resp = s.history[0]
        assert resp["status"] == 200
        assert resp["elapsed_ms"] >= 0 and resp["bytes"] > 0

    def test_records_blocked_request(self, http_server, tmp_path):
        base, _ = http_server
        s = make_session(tmp_path)
        s.get(f"{base}/forbidden")
        assert s.history[-1]["status"] == 403

    def test_records_retries(self, http_server, tmp_path):
        base, _ = http_server
        s = make_session(tmp_path)
        s.get(f"{base}/flaky")
        statuses = [h["status"] for h in s.history if h["event"] == "response"]
        assert statuses == [429, 200]
        assert [h["attempt"] for h in s.history] == [1, 2]


# --------------------------------------------------------------------------
# DebugSession report assembly
# --------------------------------------------------------------------------

class TestDebugReport:
    def test_report_structure_and_results(self, tmp_path):
        dbg = DebugSession(args={"sources": "x"})
        prefs = Preferences(max_price=3500, beds=1, k=5)
        dbg.prefs = prefs
        listings = DemoProvider(None, n=50).fetch(prefs)
        dbg.listing_count = len(listings)
        dbg.results = knn(Query.from_preferences(prefs), listings, k=5)
        dbg.provider_stats.append({"provider": "demo", "listings": 50, "error": None})

        path = dbg.save(tmp_path)
        assert path.name.startswith("run_") and path.suffix == ".json"
        report = json.loads(path.read_text())
        assert report["meta"]["args"] == {"sources": "x"}
        assert report["preferences"]["max_price"] == 3500
        assert report["listing_count"] == len(listings)
        assert len(report["results_top"]) == 5
        top = report["results_top"][0]
        assert {"rank", "id", "price", "distance", "feature_distances"} <= set(top)
        assert report["providers"][0]["provider"] == "demo"
        assert report["error"] is None

    def test_secrets_redacted_everywhere(self, tmp_path):
        secret = "rk_live_abcdef123456"
        dbg = DebugSession(args={"rentcast_key": secret})
        dbg.add_secret(secret)
        dbg.provider_stats.append(
            {"provider": "rentcast", "error": f"401 for key {secret}"})
        dbg.error = f"Traceback ... X-Api-Key: {secret}"

        text = (dbg.save(tmp_path)).read_text()
        assert secret not in text
        assert REDACTED in text
        assert secret not in (tmp_path / "latest.json").read_text()

    def test_short_or_empty_secrets_ignored(self, tmp_path):
        dbg = DebugSession()
        dbg.add_secret(None)
        dbg.add_secret("abc")  # too short — redacting would mangle the report
        text = dbg.save(tmp_path).read_text()
        assert REDACTED not in text

    def test_captures_neighbors_logs(self, tmp_path):
        dbg = DebugSession()
        logging.getLogger("neighbors.streeteasy").warning("blocked by PX")
        report = json.loads(dbg.save(tmp_path).read_text())
        assert any("blocked by PX" in line for line in report["log"])

    def test_http_history_included_when_session_attached(self, http_server, tmp_path):
        base, _ = http_server
        dbg = DebugSession()
        s = make_session(tmp_path)
        s.get(f"{base}/ok")
        dbg.session = s
        report = json.loads(dbg.save(tmp_path).read_text())
        assert report["http"]["budget_spent"] == 1
        assert report["http"]["requests"][0]["status"] == 200

    def test_each_save_creates_new_file_and_updates_latest(self, tmp_path):
        p1 = DebugSession(args={"n": 1}).save(tmp_path)
        p2 = DebugSession(args={"n": 2}).save(tmp_path)
        assert p1 != p2
        assert len(list(tmp_path.glob("run_*.json"))) == 2
        latest = json.loads((tmp_path / "latest.json").read_text())
        assert latest["meta"]["args"] == {"n": 2}

    def test_old_reports_pruned(self, tmp_path, monkeypatch):
        monkeypatch.setattr("neighbors.debug.KEEP_REPORTS", 3)
        paths = [DebugSession(args={"n": i}).save(tmp_path) for i in range(5)]
        remaining = sorted(tmp_path.glob("run_*.json"))
        assert len(remaining) == 3
        assert remaining == sorted(paths[-3:]), "must keep the newest reports"


# --------------------------------------------------------------------------
# CLI end-to-end with --debug
# --------------------------------------------------------------------------

class TestCliDebugMode:
    def test_debug_run_writes_report(self, tmp_path):
        seed_home(tmp_path)
        fake_key = "rk_test_fake123456"
        r = run_cli(["--cached", "--debug"], tmp_path,
                    extra_env={"RENTCAST_API_KEY": fake_key})
        assert r.returncode == 0, r.stderr
        assert "Debug report written to:" in r.stdout

        debug_dir = tmp_path / ".neighbors" / "debug"
        assert len(list(debug_dir.glob("run_*.json"))) == 1
        text = (debug_dir / "latest.json").read_text()
        assert fake_key not in text, "API key leaked into debug report"
        report = json.loads(text)
        assert report["error"] is None
        assert report["meta"]["args"]["cached"] is True
        assert report["preferences"]["max_price"] == 3500
        assert report["listing_count"] > 0
        assert len(report["results_top"]) > 0

    def test_debug_captures_crash_instead_of_raw_traceback(self, tmp_path):
        # No prefs on disk and no stdin → prompt raises EOFError inside the run.
        r = run_cli(["--cached", "--debug"], tmp_path, stdin="")
        assert r.returncode == 2
        assert "Crashed" in r.stdout
        report = json.loads(
            (tmp_path / ".neighbors" / "debug" / "latest.json").read_text())
        assert report["error"] and "EOFError" in report["error"]

    def test_without_debug_flag_no_report(self, tmp_path):
        seed_home(tmp_path)
        r = run_cli(["--cached"], tmp_path)
        assert r.returncode == 0
        assert not (tmp_path / ".neighbors" / "debug").exists()

    def test_consecutive_runs_accumulate_reports(self, tmp_path):
        seed_home(tmp_path)
        run_cli(["--cached", "--debug"], tmp_path)
        run_cli(["--cached", "--debug", "--like", "1"], tmp_path)
        debug_dir = tmp_path / ".neighbors" / "debug"
        assert len(list(debug_dir.glob("run_*.json"))) == 2
        latest = json.loads((debug_dir / "latest.json").read_text())
        assert latest["meta"]["args"]["like"] == "1"

    def test_debug_records_provider_stats(self, tmp_path):
        seed_home(tmp_path, with_listings=True)
        # Live sources with no key & unknown provider: stats must record both.
        r = run_cli(["--debug", "--sources", "rentcast,bogus"], tmp_path)
        report = json.loads(
            (tmp_path / ".neighbors" / "debug" / "latest.json").read_text())
        stats = {s["provider"]: s for s in report["providers"]}
        assert stats["rentcast"]["error"].startswith("no API key")
        assert stats["bogus"]["error"].startswith("unknown provider")
        # fetch produced nothing, but the seeded store still ranks fine
        assert r.returncode == 0, r.stderr
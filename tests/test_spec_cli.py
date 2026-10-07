"""Spec tests for the terminal CLI — each test cites its requirement in
docs/SPEC.md. Every run uses a throwaway HOME and never touches the network."""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request

from .conftest import PROJECT_ROOT, PROMPT_INPUT, run_cli, seed_home

NUMBERED_RESULT = re.compile(r"^\s*1[.)]?\s", re.MULTILINE)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestCliSpec:
    def test_help_names_program_and_web_command(self, tmp_path):
        """CLI-01"""
        r = run_cli(["--help"], tmp_path)
        assert r.returncode == 0
        assert r.stdout.startswith("usage: neighbors")
        assert "web" in r.stdout

    def test_module_entry_point_matches_command(self, tmp_path):
        """CLI-02: `python -m neighbors` is the same program (run_cli uses it;
        here we also check it agrees with the installed console script)."""
        script = os.path.join(os.path.dirname(sys.executable), "neighbors")
        env = {**os.environ, "HOME": str(tmp_path)}
        via_module = run_cli(["--help"], tmp_path).stdout
        via_script = subprocess.run([script, "--help"], env=env, capture_output=True,
                                    text=True, timeout=60).stdout
        assert via_module == via_script

    def test_first_run_prompts_saves_then_searches(self, tmp_path):
        """CLI-03"""
        seed_home(tmp_path)
        prefs_file = tmp_path / ".neighbors" / "preferences.json"
        prefs_file.unlink()
        r = run_cli(["--cached"], tmp_path, stdin=PROMPT_INPUT)
        assert r.returncode == 0, r.stderr
        assert "Tell me what you're looking for" in r.stdout
        saved = json.loads(prefs_file.read_text())
        assert saved["max_price"] == 3500
        assert saved["neighborhoods"] == ["williamsburg", "greenpoint"]
        assert NUMBERED_RESULT.search(r.stdout)

    def test_cached_ranks_offline_with_match_scores(self, tmp_path):
        """CLI-04: no network — HTTPS proxies point at a dead port, so any
        real request would fail loudly instead of silently succeeding."""
        seed_home(tmp_path)
        dead = "http://127.0.0.1:9"
        r = run_cli(["--cached"], tmp_path,
                    extra_env={"HTTPS_PROXY": dead, "HTTP_PROXY": dead})
        assert r.returncode == 0, r.stderr
        assert "no network" in r.stdout
        assert NUMBERED_RESULT.search(r.stdout)
        assert re.search(r"\b\d{1,3}%", r.stdout), "expected 0-100 match scores"
        assert not (tmp_path / ".neighbors" / "cache").exists()

    def test_cached_with_empty_store_explains_and_exits_cleanly(self, tmp_path):
        """CLI-05"""
        seed_home(tmp_path, with_listings=False)
        r = run_cli(["--cached"], tmp_path)
        assert r.returncode == 1
        assert "run once without --cached" in r.stdout
        assert "Traceback" not in r.stderr

    def test_prefs_mode_saves_and_does_nothing_else(self, tmp_path):
        """CLI-06"""
        r = run_cli(["--prefs"], tmp_path, stdin=PROMPT_INPUT)
        assert r.returncode == 0, r.stderr
        app_dir = tmp_path / ".neighbors"
        assert json.loads((app_dir / "preferences.json").read_text())["max_price"] == 3500
        assert "Nothing fetched" in r.stdout
        assert not (app_dir / "store.db").exists()
        assert not (app_dir / "cache").exists()

    def test_web_command_serves_page(self, tmp_path):
        """CLI-07"""
        seed_home(tmp_path)
        port = _free_port()
        env = {**os.environ, "HOME": str(tmp_path)}
        proc = subprocess.Popen(
            [sys.executable, "-m", "neighbors", "web", "--port", str(port), "--no-browser"],
            cwd=PROJECT_ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            page, deadline = None, time.monotonic() + 20
            while page is None and time.monotonic() < deadline:
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2) as resp:
                        page = resp.read().decode()
                except OSError:
                    time.sleep(0.2)
            assert page is not None, "web server never came up"
            assert "<title>Neighbors</title>" in page
        finally:
            proc.terminate()
            proc.wait(timeout=10)

    def test_removed_tuning_flags_are_rejected(self, tmp_path):
        """CLI-08"""
        for flag in ("--budget=5", "--interval=1", "--max-pages=3", "--host=0.0.0.0"):
            r = run_cli([flag], tmp_path)
            assert r.returncode == 2, flag
            assert "unrecognized arguments" in r.stderr


class TestCliRegressions:
    def test_web_port_in_use_explains_instead_of_traceback(self, tmp_path):
        with socket.socket() as busy:
            busy.bind(("127.0.0.1", 0))
            busy.listen()
            port = busy.getsockname()[1]
            r = run_cli(["web", "--port", str(port), "--no-browser"], tmp_path)
        assert r.returncode == 1
        assert "--port" in r.stdout and "Traceback" not in r.stderr

    def test_out_of_range_values_are_usage_errors(self, tmp_path):
        for args in (["--alpha", "3"], ["--alpha", "x"], ["web", "--port", "70000"]):
            r = run_cli(args, tmp_path)
            assert r.returncode == 2, args
            assert "expected a value from" in r.stderr

    def test_quota_set_used_rejects_unknown_provider(self, tmp_path):
        r = run_cli(["quota", "--set-used", "=4"], tmp_path)
        assert r.returncode == 1 and "usage: quota" in r.stdout

    def test_engine_hide_confirms_in_plain_english(self, tmp_path):
        seed_home(tmp_path)
        r = run_cli(["engine", "--cached"], tmp_path, stdin="hide 1\nq\n")
        assert r.returncode == 0, r.stderr
        assert "  hidden: " in r.stdout and "hided" not in r.stdout

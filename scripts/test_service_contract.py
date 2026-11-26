"""Offline checks for standardized selectors, failures, cleanup and rotation."""
import argparse
import contextlib
import importlib.util
import io
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]

def module(name):
    spec = importlib.util.spec_from_file_location("contract_" + name.replace("-", "_"), ROOT / "scripts" / (name + ".py"))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result

def arguments(**changes):
    values = dict(command="test", suite="unit", timeout=5, case=[], list_cases=False,
                  fail_fast=False, quiet=False, keep_data=False, retries=0, interval=0,
                  project=None, format="json", output=None, environment="test", level="liveness")
    values.update(changes)
    return argparse.Namespace(**values)

class ServiceContractTests(unittest.TestCase):
    def setUp(self):
        self.runtime = module("service-runtime")

    def test_standard_stop_timeout_names_override_legacy_aliases(self):
        self.assertEqual(self.runtime.stop_timeouts({
            "STOP_TIMEOUT_SECONDS": "7", "FORCE_STOP_TIMEOUT_SECONDS": "2",
            "TESTER_STOP_TIMEOUT": "99", "TESTER_FORCE_STOP_TIMEOUT": "88",
        }), (7, 2))

    def test_stop_timeout_defaults_legacy_names_and_validation(self):
        self.assertEqual(self.runtime.stop_timeouts({}), (30, 5))
        self.assertEqual(self.runtime.stop_timeouts({
            "TESTER_STOP_TIMEOUT": "9", "TESTER_FORCE_STOP_TIMEOUT": "1",
        }), (9, 1))
        with self.assertRaises(ValueError):
            self.runtime.stop_timeouts({"STOP_TIMEOUT_SECONDS": "-1"})

    def test_unknown_case_is_rejected_before_any_command(self):
        with patch.object(self.runtime, "run_command") as command:
            with self.assertRaises(ValueError):
                self.runtime.checks(arguments(case=["UNKNOWN"]), {})
        command.assert_not_called()

    def test_known_case_selection_returns_one_json_result(self):
        stdout = io.StringIO()
        with patch.object(self.runtime, "run_command"), contextlib.redirect_stdout(stdout):
            code = self.runtime.checks(arguments(case=["UNIT-TESTER-SCRIPTS"]), {})
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue())["summary"]["passed"], 1)

    def test_timeout_returns_four_and_preserves_failure(self):
        stdout = io.StringIO()
        with patch.object(self.runtime, "run_command", side_effect=subprocess.TimeoutExpired("unit", 1)), contextlib.redirect_stdout(stdout):
            code = self.runtime.checks(arguments(), {})
        self.assertEqual(code, 4)
        self.assertEqual(json.loads(stdout.getvalue())["summary"]["failed"], 1)

    def test_health_retry_is_explicit_and_success_clears_failure(self):
        stdout = io.StringIO()
        with patch.object(self.runtime, "get_json", side_effect=[OSError(), {"status": "pass", "unit": "tester", "source_commit": json.loads((ROOT / "release.json").read_text())["source_commit"] if (ROOT / "release.json").exists() else None}]) as check, contextlib.redirect_stdout(stdout):
            code = self.runtime.checks(arguments(command="health", retries=1), {})
        self.assertEqual(code, 0)
        self.assertEqual(check.call_count, 2)
        self.assertEqual(json.loads(stdout.getvalue())["type"], "health_check")

    def test_test_failures_are_not_retried_and_fail_fast_stops(self):
        stdout = io.StringIO()
        with patch.object(self.runtime, "get_json", side_effect=OSError()) as check, contextlib.redirect_stdout(stdout):
            code = self.runtime.checks(arguments(suite="smoke", fail_fast=True), {})
        self.assertEqual(code, 1)
        self.assertEqual(check.call_count, 1)
        self.assertEqual(json.loads(stdout.getvalue())["summary"]["total"], 1)

    def test_cleanup_failure_returns_five(self):
        stdout = io.StringIO()
        with patch.object(self.runtime, "browser_fixture", side_effect=self.runtime.CleanupError()), contextlib.redirect_stdout(stdout):
            code = self.runtime.checks(arguments(suite="integration"), {})
        self.assertEqual(code, 5)
        self.assertEqual(json.loads(stdout.getvalue())["status"], "fail")

    def test_invalid_flags_and_prod_keep_data_return_two(self):
        for flags in [("--timeout", "nan"), ("--retries", "1"),
                      ("--case", "UNKNOWN"), ("--keep-data", "--environment", "prod")]:
            result = subprocess.run([sys.executable, str(ROOT / "scripts/service-runtime.py"), "test", *flags],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 2, result.stderr)

    def test_json_and_junit_preserve_skipped_counts(self):
        value = {"status": "pass", "summary": {"total": 2, "passed": 1, "failed": 0, "skipped": 1},
                 "cases": [{"id": "one", "status": "pass", "duration_ms": 1, "message": "ok"},
                           {"id": "two", "status": "skip", "duration_ms": 0, "message": "not applicable"}]}
        self.assertEqual(json.loads(self.runtime.format_result(value, "json"))["summary"]["skipped"], 1)
        self.assertIn("<skipped ", self.runtime.format_result(value, "junit"))

    def test_rotating_logs_have_bounded_size_and_backups(self):
        supervisor = module("service-supervisor")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "control.log"
            handler = supervisor.rotating_handler(path, max_bytes=1024, backups=2)
            logger = logging.Logger("rotation-fixture")
            logger.addHandler(handler)
            try:
                for _ in range(80):
                    logger.info("x" * 100)
            finally:
                handler.close()
            self.assertTrue(path.with_suffix(".log.1").exists())
            self.assertLessEqual(len(list(Path(folder).glob("control.log*"))), 3)
            self.assertTrue(all(p.stat().st_size <= 1024 for p in Path(folder).glob("control.log*")))


    def test_timeout_stops_descendant_processes(self):
        import signal
        import time
        with tempfile.TemporaryDirectory() as folder:
            info = Path(folder) / "processes.json"
            child_code = "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(20)"
            parent_code = "import subprocess,sys,os,time,json;from pathlib import Path;p=subprocess.Popen([sys.executable,'-c'," + repr(child_code) + "]);Path(" + repr(str(info)) + ").write_text(json.dumps([os.getpid(),p.pid]));time.sleep(20)"
            group = None
            try:
                with self.assertRaises(subprocess.TimeoutExpired):
                    self.runtime.run_command([sys.executable, "-c", parent_code], os.environ.copy(), 0.5, quiet=True)
                group, descendant = json.loads(info.read_text())
                limit = time.monotonic() + 1
                while self.runtime.alive(descendant) and time.monotonic() < limit:
                    time.sleep(0.02)
                self.assertFalse(self.runtime.alive(descendant), "timed-out descendant still running")
            finally:
                if group is not None:
                    try:
                        os.killpg(group, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_tag_changes_only_release_commit_and_pushes_only_to_local_fixture(self):
        import shutil
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            origin, repo = root / "origin.git", root / "source"
            def git(*args, cwd=repo):
                return subprocess.check_output(["git", "-c", "user.name=Fixture",
                    "-c", "user.email=fixture@example.invalid", "-C", str(cwd), *args],
                    text=True, stderr=subprocess.DEVNULL).strip()
            origin.mkdir(); git("init", "--bare", cwd=origin)
            repo.mkdir(); git("init", "-b", "main")
            (repo / "scripts").mkdir()
            for name in ("tag.sh", "tag-release.py"):
                shutil.copy2(ROOT / "scripts" / name, repo / "scripts" / name)
            (repo / "package.json").write_text('{"version":"0.1.0"}')
            git("config", "user.name", "Fixture")
            git("config", "user.email", "fixture@example.invalid")
            git("add", "."); git("commit", "-m", "fixture")
            git("remote", "add", "origin", str(origin)); git("push", "-u", "origin", "main")
            baseline = git("rev-parse", "HEAD")
            subprocess.run(["bash", "scripts/tag.sh", "--bump", "minor", "--unit-version", "tester-service=7.8.9"],
                           cwd=repo, check=True, capture_output=True)
            self.assertEqual(git("rev-parse", "HEAD"), baseline)
            self.assertEqual(git("rev-parse", "refs/heads/main", cwd=origin), baseline)
            self.assertIn("Source-Commit: " + baseline, git("for-each-ref", "--format=%(contents)", "refs/tags/v0.2.0"))
            self.assertEqual(json.loads(git("show", "v0.2.0:package.json"))["version"], "0.2.0")
            self.assertEqual(git("show", "v0.2.0:scripts/artifact-versions.conf"), "tester-service 7.8.9")

    def test_e2e_report_parser_keeps_skips_and_stable_ids(self):
        report = {"suites": [{"title": "root", "specs": [
            {"title": "RT-API-001 fixture", "file": "products/router/tests/x.spec.ts", "line": 2,
             "tests": [{"projectName": "router", "results": [{"status": "skipped"}]}]}]}]}
        cases = self.runtime.specs_from_report(report)
        self.assertEqual(cases[0][0], "RT-API-001")
        self.assertEqual(cases[0][2]["results"][0]["status"], "skipped")

    def test_e2e_selection_lists_without_browsers_then_executes_in_namespace(self):
        report = {"suites": [{"specs": [
            {"title": "RT-API-999 isolation regression", "file": "fixture.spec.ts", "line": 1,
             "tests": [{"projectName": "router", "results": []}]}]}]}
        executed = []

        def run(command, env, timeout, quiet=False):
            if "--list" in command:
                # Collection must not depend on Xvfb or launch any browser.
                self.assertEqual(command[0], "node")
            else:
                # This is the direct test.sh E2E path that previously bypassed bwrap.
                self.assertEqual(command[:2], ["bash", str(ROOT / "scripts/browser-runtime.sh")])
                report["suites"][0]["specs"][0]["tests"][0]["results"] = [{"status": "passed"}]
                executed.append(command)
            Path(env["PLAYWRIGHT_JSON_OUTPUT_FILE"]).write_text(json.dumps(report))

        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / "fixture.env"
            config.write_text("")
            stdout = io.StringIO()
            with patch.object(self.runtime, "run_command", side_effect=run), contextlib.redirect_stdout(stdout):
                code = self.runtime.checks(arguments(suite="e2e", case=["RT-API-999"]),
                                           {"TESTER_ENV_FILE": str(config)})
        self.assertEqual(code, 0)
        self.assertEqual(len(executed), 1)
        self.assertEqual(json.loads(stdout.getvalue())["summary"]["passed"], 1)

    def test_upgrade_copies_private_config_without_overwriting_target(self):
        with tempfile.TemporaryDirectory() as folder:
            current, target = [Path(folder) / name for name in ("old", "new")]
            current.mkdir()
            target.mkdir()
            (current / "runtime.json").write_text('{"TESTER_CONTROL_PORT":"1"}')
            (current / "projects.json").write_text('{"projects":{}}')
            (target / "runtime.json").write_text('{"TESTER_CONTROL_PORT":"2"}')
            for _ in range(2):
                subprocess.run(["bash", str(ROOT / "scripts/upgrade.sh"), "copy-config", str(current), str(target)], check=True)
            self.assertIn('"2"', (target / "runtime.json").read_text())
            self.assertEqual((current / "projects.json").read_bytes(), (target / "projects.json").read_bytes())
            self.assertEqual((target / "projects.json").stat().st_mode & 0o777, 0o600)

    def test_upgrade_migrates_legacy_tester_env_and_projects_into_new_version(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            current, target, legacy = [root / name for name in ("old", "new", "legacy")]
            current_config = current
            target_config = target
            current_config.mkdir(parents=True)
            target_config.mkdir(parents=True)
            legacy.mkdir()

            projects = legacy / "consolidated-projects.json"
            projects.write_text('{"projects":{"router":{"enabled":true}}}')
            env_file = legacy / "tester.env"
            env_file.write_text("ROUTER_PERSONAL_UPSTREAM_KEY=fixture-secret\n")
            runtime = legacy / "runtime-artifact.json"
            runtime.write_text(json.dumps({
                "TESTER_CONTROL_CONFIG": str(projects),
                "TESTER_ENV_FILE": str(env_file),
                "TESTER_PRIVATE_REPORT_DIR": "/data/test-results-private",
                "TESTER_CONTROL_PORT": "18082",
            }))

            subprocess.run(
                ["bash", str(ROOT / "scripts/upgrade.sh"), "copy-config", str(current), str(target)],
                check=True,
                env={**os.environ, "TESTER_RUNTIME_CONFIG": str(runtime)},
            )

            expected_projects = '{"projects":{"router":{"enabled":true}}}'
            self.assertEqual((current_config / "projects.json").read_text(), expected_projects)
            self.assertEqual((target_config / "projects.json").read_text(), expected_projects)
            self.assertEqual((target_config / ".env.local").read_text(), env_file.read_text())
            self.assertEqual((current_config / ".env.local").stat().st_mode & 0o777, 0o600)
            self.assertEqual((target_config / ".env.local").stat().st_mode & 0o777, 0o600)

            migrated_runtime = json.loads((target_config / "runtime.json").read_text())
            self.assertEqual(migrated_runtime["TESTER_CONTROL_CONFIG"], "/opt/deploy/tester/projects.json")
            self.assertEqual(migrated_runtime["TESTER_ENV_FILE"], "/opt/deploy/tester/.env.local")
            self.assertEqual(migrated_runtime["TESTER_PRIVATE_REPORT_DIR"], "/data/test-results-private")
            self.assertEqual(migrated_runtime["TESTER_CONTROL_PORT"], "18082")
            self.assertEqual((target_config / "runtime.json").stat().st_mode & 0o777, 0o600)

if __name__ == "__main__":
    unittest.main()

"""Isolated checks for the Tester control queue and protected POST endpoint."""

import importlib.util
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from datetime import datetime, time as clock_time, timedelta
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from http.server import ThreadingHTTPServer


SCRIPT = Path(__file__).parent / "tester-control.py"


class TesterControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        repo = root / "router"
        (repo / ".git").mkdir(parents=True)
        config = root / "projects.json"
        config.write_text(json.dumps({"projects": {
            "router": {"repo": str(repo), "enabled": True}
        }}), encoding="utf-8")
        values = {
            "TESTER_ARTIFACT_MODE": "0",
            "TESTER_CONTROL_CONFIG": str(config),
            "TESTER_PRIVATE_REPORT_DIR": str(root / "private"),
            "TESTER_REPORT_DIR": str(root / "public"),
        }
        with patch.dict(os.environ, values):
            spec = importlib.util.spec_from_file_location("tester_control_test", SCRIPT)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        self.module = module

    def test_manual_queue_deduplicates_project(self):
        control = self.module
        first = control.enqueue("router", "manual")
        second = control.enqueue("router", "manual")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(control.STATE["pending"]), 1)

    def test_daily_schedule_queues_the_full_suite_once_per_day(self):
        control = self.module
        schedule = control.daily_schedule({
            "TESTER_DAILY_RUN_ENABLED": "true",
            "TESTER_DAILY_RUN_TIME": "03:00",
            "TESTER_DAILY_RUN_TIMEZONE": "Asia/Shanghai",
        })
        self.assertEqual(schedule, (clock_time(3, 0), ZoneInfo("Asia/Shanghai")))
        before = datetime(2026, 11, 20, 2, 59, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.assertEqual(control.next_daily_run(before, *schedule).date(), before.date())
        after = datetime(2026, 11, 20, 3, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.assertEqual(
            control.next_daily_run(after, *schedule).date(),
            before.date() + timedelta(days=1),
        )
        first = control.enqueue(control.FULL, "daily-schedule")
        second = control.enqueue(control.FULL, "daily-schedule")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(control.STATE["pending"]), 1)

    def test_daily_schedule_is_disabled_by_default_and_rejects_invalid_values(self):
        control = self.module
        self.assertIsNone(control.daily_schedule({}))
        with self.assertRaisesRegex(ValueError, "HH:MM"):
            control.daily_schedule({
                "TESTER_DAILY_RUN_ENABLED": "true",
                "TESTER_DAILY_RUN_TIME": "3am",
            })
        with self.assertRaisesRegex(ValueError, "timezone"):
            control.daily_schedule({
                "TESTER_DAILY_RUN_ENABLED": "true",
                "TESTER_DAILY_RUN_TIMEZONE": "Unknown/Timezone",
            })

    def test_manual_full_run_is_separate_from_project_and_deduplicated(self):
        control = self.module
        first = control.enqueue(control.FULL, "manual")
        self.assertEqual(control.enqueue(control.FULL, "manual")["id"], first["id"])
        control.enqueue("router", "manual")
        self.assertEqual(len(control.STATE["pending"]), 2)

    def test_manual_page_only_offers_scope_selection(self):
        page = self.module.PAGE
        self.assertIn('id="scope"', page)
        self.assertIn('id="run"', page)
        self.assertNotIn('id="mode"', page)
        self.assertNotIn('id="projects"', page)

    def test_test_only_allows_project_without_deployment_adapter(self):
        control = self.module
        control.PROJECTS["node"] = {"repo": "/tmp/node", "enabled": False}
        test = control.enqueue("node", "manual", mode="test")
        self.assertEqual(test["mode"], "test")
        with self.assertRaisesRegex(ValueError, "Deployment adapter"):
            control.enqueue("node", "manual", mode="deploy")
        deploy = control.enqueue("router", "manual", mode="deploy")
        test_same_project = control.enqueue("router", "manual", mode="test")
        self.assertNotEqual(deploy["id"], test_same_project["id"])

    def test_disabled_project_can_be_registered_without_checkout(self):
        control = self.module
        config = Path(self.temp.name) / "projects.json"
        config.write_text(json.dumps({"projects": {
            "orchestrator": {
                "repo": "/root/code/orchestrator",
                "enabled": False,
                "reason": "Deployment adapter is not configured",
            }
        }}), encoding="utf-8")
        control.CONFIG = config
        self.assertEqual(list(control.load_config()), ["orchestrator"])

    def test_test_only_runs_single_playwright_project_without_git_or_deploy(self):
        control = self.module
        control.PROJECTS["node"] = {"repo": "/tmp/node", "enabled": False}
        item = control.enqueue("node", "manual", mode="test")
        summary = {"request_id": item["id"], "run": "2026-10-17_10-00-00+0800", "status": "passed"}
        with patch.object(control, "cooldown_until", return_value=None), \
             patch.object(control, "git_sha") as git, \
             patch.object(control, "latest_summary", return_value=summary), \
             patch.object(control.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run:
            self.assertTrue(control.execute(item))
        git.assert_not_called()
        self.assertEqual(run.call_args.args[0], [str(control.TESTER_MAIN_RUNNER), "--project", "node"])
        self.assertEqual(control.STATE["recent"][0]["report"],
                         "/report/projects/node/runs/2026-10-17_10-00-00+0800/index.html")

    def test_full_run_uses_existing_runner_and_records_report(self):
        control = self.module
        item = control.enqueue(control.FULL, "manual")
        summary = {"request_id": item["id"], "run": "2026-10-17_10-00-00+0800", "status": "passed"}
        with patch.object(control, "cooldown_until", return_value=None), \
             patch.object(control, "latest_summary", return_value=summary), \
             patch.object(control.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run:
            self.assertTrue(control.execute(item))
        self.assertEqual(run.call_args.args[0], [str(control.TESTER_MAIN_RUNNER)])
        self.assertEqual(control.STATE["recent"][0]["result"], "passed")
        self.assertEqual(control.STATE["recent"][0]["report"],
                         "/report/runs/2026-10-17_10-00-00+0800/index.html")

    def test_main_change_enqueues_new_commit(self):
        control = self.module
        old = "a" * 40
        new = "b" * 40
        control.STATE["seen"]["router"] = old
        with patch.object(control, "git_sha", side_effect=[new, new]):
            control.poll_main()
        self.assertEqual(control.STATE["pending"][0]["target_sha"], new)
        self.assertEqual(control.STATE["pending"][0]["source"], "main")

    def test_artifact_mode_watches_approved_project_mains(self):
        control = self.module
        control.ARTIFACT_MODE = True
        control.ALLOW_ARTIFACT_SOURCE_DEPLOY = True
        old = "a" * 40
        new = "b" * 40
        control.STATE["seen"]["router"] = old
        with patch.object(control, "git_sha", side_effect=[new, new]):
            control.poll_main()
        item = control.STATE["pending"][0]
        self.assertEqual((item["project"], item["source"], item["target_sha"], item["mode"]),
                         ("router", "main", new, "deploy"))

    def test_artifact_mode_requires_explicit_source_deploy_permission(self):
        control = self.module
        control.ARTIFACT_MODE = True
        control.ALLOW_ARTIFACT_SOURCE_DEPLOY = False
        with self.assertRaisesRegex(ValueError, "does not deploy"):
            control.enqueue("router", "manual", mode="deploy")

    def test_artifact_mode_routes_project_main_to_source_deployment_runner(self):
        control = self.module
        control.ARTIFACT_MODE = True
        control.ALLOW_ARTIFACT_SOURCE_DEPLOY = True
        item = control.enqueue("router", "main", "b" * 40)
        summary = {"request_id": item["id"], "run": "2026-10-17_10-00-00+0800",
                   "status": "passed", "target_sha": "b" * 40}
        with patch.object(control, "cooldown_until", return_value=None), \
             patch.object(control, "git_sha", return_value="b" * 40), \
             patch.object(control, "latest_summary", return_value=summary), \
             patch.object(control.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run:
            self.assertTrue(control.execute(item))
        self.assertEqual(run.call_args.args[0], [str(control.RUNNER), "router", "b" * 40])
        self.assertEqual(control.STATE["recent"][0]["result"], "passed")

    def test_new_main_during_active_run_stays_queued(self):
        control = self.module
        active = control.enqueue("router", "manual", "a" * 40)
        control.STATE["active"] = control.STATE["pending"].pop(0)
        queued = control.enqueue("router", "main", "b" * 40)
        self.assertNotEqual(active["id"], queued["id"])
        self.assertEqual(active["target_sha"], "a" * 40)
        self.assertEqual(queued["target_sha"], "b" * 40)
        self.assertEqual(len(control.STATE["pending"]), 1)
        self.assertEqual(control.enqueue("router", "main", "c" * 40)["id"], queued["id"])
        self.assertEqual(queued["target_sha"], "c" * 40)

    def test_router_waits_for_shared_rate_limit_window(self):
        control = self.module
        attempt = control.PUBLIC / "projects/router/last-attempt.txt"
        attempt.parent.mkdir(parents=True)
        attempt.write_text("published", encoding="utf-8")
        os.utime(attempt, (1000, 1000))
        with patch.object(control.time, "time", return_value=1000):
            self.assertEqual(control.cooldown_until("router"), 2260)
            self.assertIsNone(control.cooldown_until("books"))
        with patch.object(control.time, "time", return_value=2260):
            self.assertIsNone(control.cooldown_until("router"))

    def test_deferred_project_does_not_block_ready_project(self):
        control = self.module
        control.PROJECTS["books"] = {"repo": "/tmp/books", "enabled": True}
        blocked = control.enqueue("router", "manual")
        ready = control.enqueue("books", "manual")
        blocked["retry_at"] = 2000
        with patch.object(control.time, "time", return_value=1000):
            selected = control.take_ready()
        self.assertEqual(selected["id"], ready["id"])
        self.assertEqual(control.STATE["pending"][0]["id"], blocked["id"])

    def test_new_main_commit_clears_deferred_retry(self):
        control = self.module
        item = control.enqueue("router", "main", "a" * 40)
        item["retry_at"] = 2000
        item["deferred_until"] = "later"
        control.enqueue("router", "main", "b" * 40)
        self.assertEqual(item["target_sha"], "b" * 40)
        self.assertNotIn("retry_at", item)

    def test_tester_main_change_queues_full_on_upstream_commit(self):
        control = self.module
        old = "a" * 40
        new = "b" * 40
        control.STATE["seen"][control.TESTER_MAIN_KEY] = old
        with patch.object(control, "git_sha", side_effect=[new, old]):
            control.poll_tester_main()
        item = control.STATE["pending"][0]
        self.assertEqual((item["project"], item["source"], item["target_sha"]),
                         (control.FULL, "tester-main", new))
        self.assertEqual(control.STATE["seen"][control.TESTER_MAIN_KEY], new)

    def test_new_tester_main_during_full_run_stays_queued(self):
        control = self.module
        active = control.enqueue(control.FULL, "tester-main", "a" * 40)
        control.STATE["active"] = control.STATE["pending"].pop(0)
        queued = control.enqueue(control.FULL, "tester-main", "b" * 40)
        self.assertNotEqual(active["id"], queued["id"])
        self.assertEqual(active["target_sha"], "a" * 40)
        self.assertEqual(control.enqueue(control.FULL, "tester-main", "c" * 40)["id"],
                         queued["id"])
        self.assertEqual(queued["target_sha"], "c" * 40)

    def test_tester_main_report_requires_matching_source_sha(self):
        control = self.module
        item = control.enqueue(control.FULL, "tester-main", "b" * 40)
        summary = {"request_id": item["id"], "run": "2026-10-17_10-00-00+0800",
                   "status": "passed", "tester_sha": "b" * 40}
        with patch.object(control, "cooldown_until", return_value=None), \
             patch.object(control, "latest_summary", return_value=summary), \
             patch.object(control.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run:
            self.assertTrue(control.execute(item))
        self.assertEqual(run.call_args.args[0], [str(control.TESTER_MAIN_RUNNER), "b" * 40])
        self.assertEqual(control.STATE["recent"][0]["result"], "passed")

    def test_wallet_allowlist_gates_status_and_manual_runs(self):
        control = self.module
        did = "did:yeying:wid_" + "a" * 32
        identity_document = {"id": did, "version": 1, "revision": 1, "controllers": [], "proof": {}}
        allowlist = Path(self.temp.name) / "authorized-dids.json"
        allowlist.write_text(json.dumps({"identities": [{"did": did, "identity_document": identity_document}]}), encoding="utf-8")
        allowlist.chmod(0o600)
        control.AUTHORIZED_DIDS_FILE = allowlist
        server = ThreadingHTTPServer(("127.0.0.1", 0), control.Handler)
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}"
        origin = {"Origin": control.ORIGIN}
        with self.assertRaises(HTTPError) as error:
            urlopen(base + "/api/status", timeout=2)
        self.assertEqual(error.exception.code, 401)

        with patch.object(control, "verify_wallet_presentation", return_value=did) as verify:
            session_request = Request(base + "/api/auth/identity/login/session", data=b"{}", method="POST", headers=origin)
            with urlopen(session_request, timeout=2) as response:
                login = json.load(response)["data"]
            verify_request = Request(
                base + "/api/auth/identity/login/verify",
                data=json.dumps({"session_id": login["session_id"], "presentation": {"holder": did}}).encode(),
                method="POST", headers={**origin, "Content-Type": "application/json"},
            )
            with urlopen(verify_request, timeout=2) as response:
                cookie = response.headers["Set-Cookie"].split(";", 1)[0]
                self.assertIn("HttpOnly", response.headers["Set-Cookie"])
                self.assertIn("Secure", response.headers["Set-Cookie"])
            self.assertEqual(verify.call_args.args[3], identity_document)
            auth_request = Request(base + "/api/auth/status", headers={"Cookie": cookie})
            with urlopen(auth_request, timeout=2) as response:
                auth = json.load(response)
            self.assertEqual(auth["did"], did)
            self.assertTrue(auth["csrf"])

            status_request = Request(base + "/api/status", headers={"Cookie": cookie})
            with urlopen(status_request, timeout=2) as response:
                status = json.load(response)
            self.assertIn("router", status["projects"])

            run_url = base + "/api/run"
            body = b'{"project":"router"}'
            missing_csrf = Request(run_url, data=body, method="POST", headers={**origin, "Cookie": cookie})
            with self.assertRaises(HTTPError) as error:
                urlopen(missing_csrf, timeout=2)
            self.assertEqual(error.exception.code, 403)
            run_request = Request(
                run_url, data=body, method="POST",
                headers={**origin, "Cookie": cookie, "X-Tester-CSRF": auth["csrf"]},
            )
            with urlopen(run_request, timeout=2) as response:
                self.assertEqual(response.status, 202)

            status_request = Request(base + "/api/status", headers={"Cookie": cookie})
            with urlopen(status_request, timeout=2) as response:
                queued_status = json.load(response)
            self.assertEqual(queued_status["projects"]["router"]["busy"], "已排队")
            self.assertNotIn("log", queued_status["projects"]["router"]["last"] or {})

            logout = Request(base + "/api/auth/logout", data=b"", method="POST", headers={
                **origin, "Cookie": cookie, "X-Tester-CSRF": auth["csrf"],
            })
            with urlopen(logout, timeout=2):
                pass
            with self.assertRaises(HTTPError) as error:
                urlopen(auth_request, timeout=2)
            self.assertEqual(error.exception.code, 401)

            empty = Path(self.temp.name) / "empty-allowlist.json"
            empty.write_text('{"identities":[]}', encoding="utf-8")
            empty.chmod(0o600)
            control.AUTHORIZED_DIDS_FILE = empty
            with patch.object(control, "verify_wallet_presentation", return_value=did):
                session_request = Request(base + "/api/auth/identity/login/session", data=b"{}", method="POST", headers=origin)
                with urlopen(session_request, timeout=2) as response:
                    login = json.load(response)["data"]
                verify_request = Request(
                    base + "/api/auth/identity/login/verify",
                    data=json.dumps({"session_id": login["session_id"], "presentation": {"holder": did}}).encode(),
                    method="POST", headers={**origin, "Content-Type": "application/json"},
                )
                with self.assertRaises(HTTPError) as error:
                    urlopen(verify_request, timeout=2)
                self.assertEqual(error.exception.code, 403)

    def test_wallet_presentation_signature_verifier(self):
        result = __import__("subprocess").run(
            ["node", str(Path(__file__).parent / "test-wallet-presentation.mjs")],
            capture_output=True, text=True, timeout=10, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("signatures and challenge verification passed", result.stdout)


class ArtifactModeBoundaryTests(unittest.TestCase):
    setUp = TesterControlTests.setUp

    def test_upstream_polling_can_be_disabled_in_artifact_mode(self):
        control = self.module
        with patch.object(control, "ARTIFACT_MODE", True), \
             patch.object(control, "WATCH_UPSTREAM_MAIN", False), \
             patch.object(control, "git_sha") as git:
            control.poll_main()
            control.poll_tester_main()
        git.assert_not_called()
        self.assertFalse(control.STATE["pending"])

    def test_artifact_mode_uses_only_packaged_runner(self):
        control = self.module
        with patch.object(control, "ARTIFACT_MODE", True):
            with self.assertRaises(ValueError):
                control.enqueue("router", "manual", mode="deploy")
            item = control.enqueue("router", "manual", mode="test")
            summary = {"request_id": item["id"], "run": "fixture", "status": "passed"}
            with patch.object(control, "cooldown_until", return_value=None), patch.object(control, "git_sha") as git, patch.object(control, "latest_summary", return_value=summary), patch.object(control.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run:
                self.assertTrue(control.execute(item))
            git.assert_not_called()
            self.assertEqual(run.call_args.args[0], [str(control.FULL_RUNNER), "router"])

if __name__ == "__main__":
    unittest.main()

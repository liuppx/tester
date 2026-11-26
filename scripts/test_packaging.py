"""Offline package-contract tests; fixture tools never call product services."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]

def module(name):
    spec = importlib.util.spec_from_file_location("tester_" + name.replace("-", "_"), ROOT / "scripts" / (name + ".py"))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result

class PackageContractTests(unittest.TestCase):
    def test_runtime_defaults_to_external_artifact_before_release_local_config(self):
        runtime = module("service-runtime")
        with tempfile.TemporaryDirectory() as folder:
            package_config = Path(folder) / "runtime.json"
            package_config.write_text('{"TESTER_CONTROL_PORT":"18083"}')
            with patch.object(runtime, "ROOT", Path(folder)), patch.object(
                runtime, "STANDARD_RUNTIME_CONFIG", package_config
            ), patch.object(runtime, "systemd_runtime_config", return_value=None):
                result = runtime.environment(required=False)
        self.assertEqual(result["TESTER_CONTROL_PORT"], "18083")

    def test_runtime_configuration_never_accepts_arbitrary_environment_keys(self):
        runtime = module("service-runtime")
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / "runtime.json"
            config.write_text('{"PATH":"/untrusted"}')
            with patch.dict(os.environ, {"TESTER_RUNTIME_CONFIG": str(config)}):
                with self.assertRaises(ValueError):
                    runtime.environment()

    def test_report_formats_preserve_failures_and_skip_counts(self):
        runtime = module("service-runtime")
        value = {"summary": {"total": 1, "failed": 1, "skipped": 0},
                 "cases": [{"id": "SMOKE-TESTER-001", "status": "fail", "duration_ms": 20, "message": "failure"}]}
        self.assertEqual(json.loads(runtime.format_result(value, "json"))["summary"]["failed"], 1)
        self.assertIn('<failure ', runtime.format_result(value, "junit"))
        self.assertIn("[FAIL]", runtime.format_result(value, "text"))

    def test_package_filter_excludes_secrets_caches_and_local_state(self):
        build = module("package-release")
        for name in ("scripts/__pycache__/x.pyc", "scripts/.env.local", ".project/task.json", "x.log"):
            self.assertFalse(build.publishable(Path(name)))
        self.assertTrue(build.publishable(Path("node_modules/.bin/tsx")))
        for name in ("config/runtime.json.template", "config/backup.conf.template",
                     ".env.template", "products/router/tests/smoke.spec.ts"):
            self.assertTrue(build.publishable(Path(name)))

    def test_release_manifest_separates_service_and_static_artifacts(self):
        build = module("package-release")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            service = root / "service.tar.gz"
            web = root / "web.tar.gz"
            service.write_bytes(b"service")
            web.write_bytes(b"static")
            manifest = build.release_manifest("a" * 40, "0.2.0", "0.2.1", service,
                                              "tester-service-v0.2.0-aaaaaaa", web,
                                              "tester-web-v0.2.1-aaaaaaa")
        artifacts = {entry["unit_id"]: entry for entry in manifest["artifacts"]}
        self.assertEqual(set(artifacts), {"tester-service", "tester-web"})
        self.assertEqual(artifacts["tester-web"]["type"], "static")
        self.assertEqual(artifacts["tester-web"]["static_targets"][0]["site_id"], "tester-web")
        self.assertEqual(artifacts["tester-service"]["type"], "service")

    def test_release_tag_accepts_independent_web_artifact_version(self):
        release = module("tag-release")
        self.assertEqual(release.parse_unit_versions([
            "tester-service=0.2.0", "tester-web=0.3.0",
        ]), {"tester-service": "0.2.0", "tester-web": "0.3.0"})
        with self.assertRaisesRegex(ValueError, "duplicate"):
            release.parse_unit_versions(["tester-web=0.2.0", "tester-web=0.3.0"])

    def test_browser_build_cache_is_persistent_and_overridable(self):
        build = module("package-release")
        self.assertTrue(str(build.browser_cache_path()).endswith(".cache/yeying/tester/playwright"))
        with patch.dict(os.environ, {"TESTER_BUILD_BROWSER_CACHE": "/tmp/tester-browser-cache"}):
            self.assertEqual(build.browser_cache_path(), Path("/tmp/tester-browser-cache"))

    def test_browser_bundle_copies_only_current_playwright_revisions(self):
        build = module("package-release")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            checkout = root / "checkout"
            manifest = checkout / "node_modules/playwright-core"
            manifest.mkdir(parents=True)
            (manifest / "browsers.json").write_text(json.dumps({"browsers": [
                {"name": "chromium", "revision": "1255", "installByDefault": True},
                {"name": "chromium-headless-shell", "revision": "1255", "installByDefault": True},
                {"name": "ffmpeg", "revision": "1013", "installByDefault": True},
                {"name": "firefox", "revision": "1572", "installByDefault": True},
                {"name": "chromium", "revision": "old", "installByDefault": False},
            ]}))
            cache = root / "cache"
            for name in ("chromium-1255", "ffmpeg-1013", "chromium-headless-shell-1255", "firefox-1572", "chromium-old"):
                (cache / name).mkdir(parents=True)
                (cache / name / "marker").write_text(name)
            destination = root / "browsers"
            destination.mkdir()
            (destination / "stale").mkdir()
            names = build.required_browser_directories(checkout)
            build.copy_browser_bundle(cache, destination, names)
            self.assertEqual(names, ["chromium-1255", "ffmpeg-1013"])
            self.assertFalse((destination / "stale").exists())
            self.assertTrue((destination / "chromium-1255/marker").exists())

    def test_browser_install_skips_unused_headless_shell(self):
        build = module("package-release")
        self.assertEqual(build.browser_install_command(), [
            "node", "node_modules/@playwright/test/cli.js", "install", "chromium", "--no-shell"
        ])

    def test_internal_bin_links_are_copied_as_executable_files(self):
        build = module("package-release")
        with tempfile.TemporaryDirectory() as folder:
            staged = Path(folder) / "release"
            target = staged / "node_modules/tsx/dist/cli.mjs"
            target.parent.mkdir(parents=True)
            target.write_text("#!/usr/bin/env node\n")
            link = staged / "node_modules/.bin/tsx"
            link.parent.mkdir(parents=True)
            link.symlink_to("../tsx/dist/cli.mjs")
            build.make_release_links_portable(staged)
            self.assertTrue(link.is_file())
            self.assertFalse(link.is_symlink())
            self.assertTrue(link.stat().st_mode & 0o111)

    def test_package_build_defines_domestic_defaults(self):
        build = module("package-release")
        self.assertEqual(build.DEFAULT_NPM_REGISTRY, "https://registry.npmmirror.com")
        self.assertEqual(build.DEFAULT_PLAYWRIGHT_DOWNLOAD_HOST,
                         "https://npmmirror.com/mirrors/playwright")

    def test_release_dependencies_use_npm_without_a_lockfile(self):
        build = module("package-release")
        self.assertIn("package.json", build.FILES)
        self.assertNotIn("web", build.DIRS)
        self.assertTrue((ROOT / "web/index.html").is_file())
        self.assertTrue((ROOT / "web/report.html").is_file())
        self.assertTrue((ROOT / "web/assets/report.js").is_file())
        self.assertTrue((ROOT / "web/assets/control.js").is_file())
        self.assertTrue((ROOT / "web/package-lock.json").is_file())
        self.assertIn("vite", json.loads((ROOT / "web/package.json").read_text())["devDependencies"])
        self.assertFalse(any("lock" in name.lower() for name in build.FILES))
        manifest = json.loads((ROOT / "package.json").read_text())
        self.assertNotIn("packageManager", manifest)
        self.assertEqual(manifest.get("engines"), {"node": ">=24.18.0"})
        with patch.object(build.subprocess, "check_output", return_value="11.0.0\n") as version:
            with patch.object(build, "run") as run:
                result = build.install_dependencies(Path("/tmp/tester-checkout"), {"CI": "1"})
        self.assertEqual(result, "11.0.0")
        version.assert_called_once_with(["npm", "--version"], text=True)
        run.assert_called_once_with(
            ["npm", "install", "--package-lock=false", "--include=dev"],
            cwd=Path("/tmp/tester-checkout"), env={"CI": "1"})

    def test_web_build_uses_its_own_locked_dependencies(self):
        build = module("package-release")
        with patch.object(build, "run") as run:
            build.build_web(Path("/tmp/tester-checkout"), {"CI": "1"})
        self.assertEqual([call.args[0] for call in run.call_args_list], [
            ["npm", "ci", "--no-audit", "--no-fund"],
            ["npm", "run", "build"],
            ["npm", "prune", "--omit=dev", "--no-audit", "--no-fund"],
        ])
        self.assertEqual([call.kwargs["cwd"] for call in run.call_args_list], [
            Path("/tmp/tester-checkout/web"), Path("/tmp/tester-checkout/web"),
            Path("/tmp/tester-checkout/web")])

    def test_invalid_release_version_is_rejected_before_building(self):
        proc = subprocess.run(["bash", str(ROOT / "scripts/package.sh"), "--ref", "HEAD",
                               "--source-commit", "0" * 40, "--version", "../../invalid", "--output-dir", "/invalid"],
                              capture_output=True, text=True)
        self.assertNotEqual(proc.returncode, 0)

    def test_stale_or_foreign_pid_is_never_owned(self):
        runtime = module("service-runtime")
        self.assertFalse(runtime.owned(os.getpid()))


class ArtifactServiceTests(unittest.TestCase):
    def test_isolated_service_can_start_smoke_and_stop_without_git_or_products(self):
        import socket
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            projects = root / "projects.json"
            projects.write_text(json.dumps({"projects": {"router": {"repo": "/not-a-checkout", "enabled": False}}}))
            config = root / "runtime.json"
            config.write_text(json.dumps({
                "TESTER_CONTROL_CONFIG": str(projects),
                "TESTER_CONTROL_HOST": "127.0.0.1",
                "TESTER_CONTROL_PORT": str(port),
                "TESTER_PRIVATE_REPORT_DIR": str(root / "private"),
                "TESTER_REPORT_DIR": str(root / "reports"),
                "TESTER_PID_FILE": str(root / "control.pid"),
                "TESTER_LOCK_FILE": str(root / "test.lock"),
                "TESTER_CONTROL_LOG": str(root / "control.log"),
            }))
            env = dict(os.environ, TESTER_RUNTIME_CONFIG=str(config))
            def run(*args):
                return subprocess.run(["bash", str(ROOT / "scripts" / args[0]), *args[1:]],
                                      env=env, capture_output=True, text=True, timeout=20)
            try:
                started = run("starter.sh", "start")
                self.assertEqual(started.returncode, 0, started.stderr)
                smoke = run("test.sh", "--suite", "smoke", "--format", "json")
                self.assertEqual(smoke.returncode, 0, f"{smoke.stderr}\n{smoke.stdout}")
                restarted = run("starter.sh", "restart")
                self.assertEqual(restarted.returncode, 0, restarted.stderr)
                report = json.loads(smoke.stdout)
                self.assertEqual(report["summary"], {"total": 3, "passed": 3, "failed": 0, "skipped": 0})
                path = root / "private/control/state.json"
                occupied = dict(env, TESTER_PID_FILE=str(root / "different.pid"))
                other = subprocess.run(["bash", str(ROOT / "scripts/starter.sh"), "start"], env=occupied, capture_output=True, timeout=20)
                self.assertNotEqual(other.returncode, 0)
                self.assertFalse((root / "different.pid").exists())
                state = json.loads(path.read_text()) if path.exists() else {}
                self.assertFalse(state.get("pending"))
                self.assertFalse(state.get("poll_errors"))
            finally:
                stopped = run("starter.sh", "stop")
                self.assertEqual(stopped.returncode, 0, stopped.stderr)
                self.assertFalse((root / "control.pid").exists())

    def test_service_stop_is_deferred_while_test_lock_is_held(self):
        import fcntl
        import socket
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            projects = root / "projects.json"
            projects.write_text(json.dumps({"projects": {"router": {"repo": "/not-a-checkout", "enabled": False}}}))
            config = root / "runtime.json"
            config.write_text(json.dumps({
                "TESTER_CONTROL_CONFIG": str(projects),
                "TESTER_CONTROL_HOST": "127.0.0.1",
                "TESTER_CONTROL_PORT": str(port),
                "TESTER_PRIVATE_REPORT_DIR": str(root / "private"),
                "TESTER_REPORT_DIR": str(root / "reports"),
                "TESTER_PID_FILE": str(root / "control.pid"),
                "TESTER_LOCK_FILE": str(root / "test.lock"),
                "TESTER_CONTROL_LOG": str(root / "control.log"),
            }))
            env = dict(os.environ, TESTER_RUNTIME_CONFIG=str(config))
            def run(*args):
                return subprocess.run(["bash", str(ROOT / "scripts" / args[0]), *args[1:]],
                                      env=env, capture_output=True, text=True, timeout=20)
            started = run("starter.sh", "start")
            self.assertEqual(started.returncode, 0, started.stderr)
            lock = (root / "test.lock").open("a")
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                stopped = run("starter.sh", "stop")
                self.assertNotEqual(stopped.returncode, 0)
                health = run("health-check.sh", "--level", "liveness", "--format", "json")
                self.assertEqual(health.returncode, 0, health.stderr)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
                lock.close()
                stopped = run("starter.sh", "stop")
                self.assertEqual(stopped.returncode, 0, stopped.stderr)

if __name__ == "__main__":
    unittest.main()

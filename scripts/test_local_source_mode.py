import json
import importlib.util
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def load_manager():
    path = ROOT / "scripts/local-source-services.py"
    spec = importlib.util.spec_from_file_location("tester_local_source_services", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LocalSourceModeTests(unittest.TestCase):
    def test_dependency_manifest_covers_safe_full_projects(self):
        manifest = json.loads((ROOT / "config/local-test-dependencies.json").read_text())
        self.assertEqual(len(manifest["full_run_projects"]), 11)
        for project in manifest["full_run_projects"]:
            self.assertIn(project, manifest["projects"])
            self.assertTrue(manifest["projects"][project]["repositories"])

    def test_product_dependency_closures_include_cross_product_services(self):
        manifest = json.loads((ROOT / "config/local-test-dependencies.json").read_text())
        self.assertIn("router", manifest["projects"]["chat"]["repositories"])
        self.assertIn("warehouse", manifest["projects"]["chat"]["repositories"])
        self.assertIn("warehouse-api", manifest["projects"]["project"]["services"])
        self.assertIn("node-api", manifest["projects"]["wallet"]["services"])

    def test_local_manager_and_readiness_have_help(self):
        for script in ("scripts/local-source-services.py", "scripts/local_test_readiness.py"):
            result = subprocess.run(["python3", str(ROOT / script), "--help"], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        result = subprocess.run(["bash", "-n", str(ROOT / "scripts/run-full-on-test-machine.sh")], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_local_manager_uses_domestic_package_mirrors(self):
        manager = load_manager()
        with tempfile.TemporaryDirectory() as folder:
            env = manager.service_env({})
            manager.configure_maven_mirror(Path(folder), env)
            self.assertEqual(env["NPM_CONFIG_REGISTRY"], "https://registry.npmmirror.com")
            self.assertEqual(env["GOPROXY"], "https://goproxy.cn,direct")
            self.assertIn("pypi.tuna.tsinghua.edu.cn", env["UV_INDEX_URL"])
            self.assertIn("aliyun-public", Path(env["MAVEN_ARGS"].split(" ", 1)[1]).read_text())

    def test_unlocked_npm_dependencies_use_legacy_peer_resolution(self):
        manager = load_manager()
        with tempfile.TemporaryDirectory() as folder:
            repo = Path(folder) / "node"
            repo.mkdir()
            (repo / "package.json").write_text('{"name":"node-test"}\n')
            with unittest.mock.patch.object(manager.subprocess, "run") as run:
                manager.ensure_npm_dependencies("node", repo, repo, Path(folder) / "runtime", {}, Path(folder) / "logs")
            self.assertEqual(run.call_args.kwargs["env"]["NPM_CONFIG_LEGACY_PEER_DEPS"], "true")

    def test_go_build_disables_worktree_vcs_stamp_only(self):
        source = (ROOT / "scripts/local-source-services.py").read_text()
        self.assertIn('"router": ["go", "build", "-buildvcs=false"', source)
        self.assertIn('"warehouse": ["go", "build", "-buildvcs=false"', source)

    def test_local_config_copy_changes_only_requested_keys(self):
        manager = load_manager()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.yaml"
            target = root / "runtime/config.yaml"
            source.write_text("server:\n  address: \\\"0.0.0.0\\\"\ndatabase:\n  password: keep-this-secret\n")
            manager.private_yaml_copy(source, target, {"server": {"address": "127.0.0.1", "port": 16065}})
            result = target.read_text()
            self.assertIn('address: "127.0.0.1"', result)
            self.assertIn("port: 16065", result)
            self.assertIn("password: keep-this-secret", result)
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_public_local_source_docs_do_not_contain_ip_addresses(self):
        for path in (ROOT / "docs/local-source-mode.md", ROOT / "config/local-test-dependencies.json"):
            self.assertIsNone(re.search(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", path.read_text()), path)

    def test_main_worktree_is_detached_from_dirty_original_checkout(self):
        manager = load_manager()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source_root = root / "code"
            original = source_root / "node"
            original.mkdir(parents=True)
            bare = root / "origin.git"
            subprocess.run(["git", "init", "--initial-branch=main", str(original)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(original), "config", "user.email", "tester@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(original), "config", "user.name", "Tester"], check=True)
            (original / "source.txt").write_text("main source\n")
            (original / "config.js").write_text("module.exports = {}\n")
            subprocess.run(["git", "-C", str(original), "add", "source.txt"], check=True)
            subprocess.run(["git", "-C", str(original), "commit", "-m", "main source"], check=True, capture_output=True)
            subprocess.run(["git", "clone", "--bare", str(original), str(bare)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(original), "remote", "add", "origin", str(bare)], check=True)
            head = subprocess.check_output(["git", "-C", str(original), "rev-parse", "HEAD"], text=True).strip()
            subprocess.run(["git", "-C", str(original), "update-ref", "refs/remotes/origin/main", head], check=True)
            (original / "source.txt").write_text("local change stays here\n")

            ports = dict(manager.DEFAULT_PORTS)
            worktree = manager.source_checkout("node", source_root, root / "managed", head, ports, {})
            sha = head

            self.assertEqual((worktree / "source.txt").read_text(), "main source\n")
            self.assertEqual((original / "source.txt").read_text(), "local change stays here\n")
            self.assertFalse((worktree / "config.js").is_symlink())
            self.assertIn(str(original / "config.js"), (worktree / "config.js").read_text())
            self.assertEqual(manager.git(worktree, "rev-parse", "HEAD"), sha)

    def test_router_e2e_dsn_rewrites_database_without_exposing_credentials(self):
        manager = load_manager()
        source = "postgres://router_app:keep-secret@127.0.0.1:5432/router?sslmode=disable"
        result = manager.dsn_with_database(source, "tester_router_e2e_a1b2c3d4")
        self.assertEqual(result, "postgres://router_app:keep-secret@127.0.0.1:5432/tester_router_e2e_a1b2c3d4?sslmode=disable")
        self.assertNotIn("keep-secret", manager.dsn_with_database(source, "tester_router_e2e_a1b2c3d4").split("@")[-1])
        with self.assertRaisesRegex(RuntimeError, "invalid isolated"):
            manager.dsn_with_database(source, "router")

    def test_router_private_config_points_to_the_ephemeral_database(self):
        manager = load_manager()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.yaml"
            target = root / "runtime/config.yaml"
            source.write_text(
                'server:\n  address: "0.0.0.0"\n  port: 3011\n'
                'database:\n  sql_dsn: "postgres://router_app:keep-secret@127.0.0.1:5432/router?sslmode=disable"\n'
            )
            manager.private_yaml_copy(source, target, {
                "server": {"address": "127.0.0.1", "port": 13011},
                "database": {"sql_dsn": manager.dsn_with_database(
                    manager.yaml_scalar(source, "database", "sql_dsn"),
                    "tester_router_e2e_a1b2c3d4",
                )},
            })
            result = target.read_text()
            self.assertIn("tester_router_e2e_a1b2c3d4", result)
            self.assertIn("keep-secret", result)
            self.assertNotIn("127.0.0.1:5432/router?", result)
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()

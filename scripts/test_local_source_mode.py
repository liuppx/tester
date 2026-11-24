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
            worktree = manager.source_checkout("node", source_root, root / "managed", head, ports)
            sha = head

            self.assertEqual((worktree / "source.txt").read_text(), "main source\n")
            self.assertEqual((original / "source.txt").read_text(), "local change stays here\n")
            self.assertFalse((worktree / "config.js").is_symlink())
            self.assertIn(str(original / "config.js"), (worktree / "config.js").read_text())
            self.assertEqual(manager.git(worktree, "rev-parse", "HEAD"), sha)


if __name__ == "__main__":
    unittest.main()

"""Config backups contain private config only and preserve verifiable copies."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "config-backup.py"
SPEC = importlib.util.spec_from_file_location("tester_config_backup", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ConfigBackupTests(unittest.TestCase):
    def test_runtime_and_referenced_configs_are_backed_up_private_and_checksummed(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            runtime = root / "runtime.json"
            projects = root / "projects.json"
            env_file = root / ".env.local"
            private_report = root / "reports"
            backup_root = root / "backups"
            archive_root = root / "opt-backup"
            projects.write_text('{"projects":{}}\n')
            env_file.write_text("TESTER_TOKEN=secret-value\n")
            runtime.write_text(json.dumps({
                "TESTER_CONTROL_CONFIG": str(projects),
                "TESTER_ENV_FILE": str(env_file),
                "TESTER_PRIVATE_REPORT_DIR": str(private_report),
                "TESTER_CONFIG_BACKUP_DIR": str(backup_root),
                "TESTER_CONFIG_ARCHIVE_DIR": str(archive_root),
            }))
            with patch.object(MODULE, "STANDARD_CONFIG_FILES", ()), patch.dict(os.environ, {
                "TESTER_RUNTIME_CONFIG": str(runtime),
                "TESTER_CONFIG_BACKUP_DIR": "",
                "TESTER_CONFIG_ARCHIVE_DIR": "",
                "TESTER_BACKUP_ROOT": "",
            }, clear=False):
                archive = MODULE.backup()

            self.assertEqual(archive.parent, archive_root)
            self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
            snapshots = list(backup_root.glob("tester-config-*"))
            self.assertEqual(len(snapshots), 1)
            snapshot = snapshots[0]
            self.assertEqual(snapshot.stat().st_mode & 0o777, 0o700)
            self.assertEqual((snapshot / "runtime.json").read_bytes(), runtime.read_bytes())
            self.assertEqual((snapshot / "projects.json").read_bytes(), projects.read_bytes())
            self.assertEqual((snapshot / ".env.local").read_bytes(), env_file.read_bytes())
            self.assertFalse((snapshot / "reports").exists())
            self.assertTrue(all(path.stat().st_mode & 0o777 == 0o600 for path in snapshot.iterdir()))
            metadata = json.loads((snapshot / "manifest.json").read_text())
            self.assertEqual(len(metadata["files"]), 3)
            self.assertNotIn("secret-value", (snapshot / "manifest.json").read_text())
            with tarfile.open(archive) as compressed:
                names = compressed.getnames()
                self.assertIn(f"{snapshot.name}/runtime.json", names)
                self.assertIn(f"{snapshot.name}/manifest.json", names)
                self.assertIn("secret-value", compressed.extractfile(f"{snapshot.name}/.env.local").read().decode())

    def test_backup_rejects_symlinked_configuration(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            secret = root / "secret"
            secret.write_text("private\n")
            link = root / "runtime.json"
            link.symlink_to(secret)
            with patch.dict(os.environ, {
                "TESTER_RUNTIME_CONFIG": str(link),
                "TESTER_CONFIG_BACKUP_DIR": str(root / "backups"),
                "TESTER_CONFIG_ARCHIVE_DIR": str(root / "archives"),
            }, clear=False):
                with self.assertRaisesRegex(ValueError, "regular file"):
                    MODULE.backup()

    def test_uses_packaged_service_runtime_artifact_without_environment(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            runtime = root / "runtime-artifact.json"
            backup_root = root / "backups"
            runtime.write_text(json.dumps({
                "TESTER_CONFIG_BACKUP_DIR": str(backup_root),
                "TESTER_CONFIG_ARCHIVE_DIR": str(root / "archives"),
                "TESTER_CONTROL_PORT": "18082",
            }))
            with patch.object(MODULE, "systemd_runtime_path", return_value=None), patch.object(
                MODULE, "STANDARD_RUNTIME_CONFIGS", (runtime,)
            ), patch.dict(
                os.environ,
                {
                    "TESTER_RUNTIME_CONFIG": "",
                    "TESTER_CONFIG_BACKUP_DIR": "",
                    "TESTER_CONFIG_ARCHIVE_DIR": "",
                    "TESTER_BACKUP_ROOT": "",
                },
                clear=False,
            ):
                archive = MODULE.backup()

            with tarfile.open(archive) as compressed:
                self.assertTrue(any(name.endswith("/runtime.json") for name in compressed.getnames()))
            snapshot = next((root / "backups").glob("tester-config-*"))
            self.assertEqual(
                json.loads((snapshot / "manifest.json").read_text())["files"][0]["source"],
                str(runtime.resolve()),
            )

    def test_uses_runtime_path_from_systemd_when_it_is_not_the_standard_location(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            runtime = root / "runtime.json"
            runtime.write_text(json.dumps({"TESTER_CONTROL_PORT": "18082"}))
            with patch.object(MODULE, "STANDARD_RUNTIME_CONFIGS", ()), patch.dict(
                os.environ,
                {"TESTER_RUNTIME_CONFIG": "", "TESTER_CONFIG_BACKUP_DIR": str(root / "backups")},
                clear=False,
            ), patch.object(
                MODULE.subprocess,
                "run",
                return_value=subprocess.CompletedProcess(
                    ["systemctl"], 0,
                    stdout=f"TESTER_RUNTIME_CONFIG={runtime} PYTHONUNBUFFERED=1\n",
                    stderr="",
                ),
            ):
                self.assertEqual(MODULE.runtime_config_path(), runtime)

    def test_includes_tester_backup_and_report_access_configuration(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            runtime = root / "runtime.json"
            backup_policy = root / "backup.conf"
            nginx = root / "tester.conf"
            access = root / ".htpasswd-tester-control"
            backup_policy.write_text("BACKUP_CONF_FLAG=False\n")
            nginx.write_text("server {}\n")
            access.write_text("tester:$apr1$hash\n")
            runtime.write_text(json.dumps({"TESTER_CONTROL_PORT": "18082"}))
            with patch.object(
                MODULE,
                "STANDARD_CONFIG_FILES",
                (backup_policy, nginx, access),
            ), patch.object(MODULE, "SYSTEMD_UNITS", ()), patch.object(
                MODULE, "STANDARD_RUNTIME_CONFIGS", (runtime,)
            ), patch.dict(
                os.environ,
                {"TESTER_RUNTIME_CONFIG": str(runtime)},
                clear=False,
            ):
                names = {name for name, _ in MODULE.configured_files()}
            self.assertIn("backup.conf", names)
            self.assertIn("tester.conf", names)
            self.assertIn(".htpasswd-tester-control", names)


if __name__ == "__main__":
    unittest.main()

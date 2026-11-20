#!/usr/bin/env python3
"""Create a private, checksummed backup of Tester runtime configuration."""

from __future__ import annotations

from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[1]
STANDARD_RUNTIME_CONFIGS = (
    Path("/data/test-results-private/control/runtime-artifact.json"),
    Path("/opt/deploy/tester/config/runtime.json"),
)
CONFIG_KEYS = {
    "TESTER_CONTROL_CONFIG",
    "TESTER_ENV_FILE",
    "TESTER_FEISHU_CONFIG",
    "TESTER_REPORT_ACCESS_KEY_FILE",
}


def digest(path: Path) -> str:
    checksum = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(block)
    return checksum.hexdigest()


def regular_file(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"configuration source is not a regular file: {path}")
    if info.st_mode & 0o002:
        raise ValueError(f"configuration source is world-writable: {path}")
    return True


def systemd_runtime_path() -> Path | None:
    try:
        result = subprocess.run(
            ["systemctl", "show", "tester-control.service", "--property=Environment", "--value"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode:
        return None
    try:
        assignments = shlex.split(result.stdout)
    except ValueError as error:
        raise ValueError("Tester systemd environment is malformed") from error
    for assignment in assignments:
        if assignment.startswith("TESTER_RUNTIME_CONFIG="):
            path = Path(assignment.split("=", 1)[1])
            if not regular_file(path):
                raise FileNotFoundError("Tester systemd runtime file is missing")
            return path
    return None


def runtime_config_path() -> Path | None:
    """Find the protected runtime file used by a packaged Tester service.

    Deployer invokes this script without loading the service's environment. The
    systemd unit therefore keeps the runtime artifact in the standard external
    control directory; the package-local config remains the development and
    first-install fallback.
    """
    configured = os.environ.get("TESTER_RUNTIME_CONFIG", "")
    if configured:
        path = Path(configured)
        if not regular_file(path):
            raise FileNotFoundError("configured Tester runtime file is missing")
        return path
    configured = systemd_runtime_path()
    if configured is not None:
        return configured
    for path in (*STANDARD_RUNTIME_CONFIGS, ROOT / "config/runtime.json"):
        if regular_file(path):
            return path
    return None


def load_runtime(path: Path) -> dict[str, str]:
    runtime_data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(runtime_data, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in runtime_data.items()
    ):
        raise ValueError("runtime configuration must contain string values")
    return runtime_data


def configured_files() -> list[tuple[str, Path]]:
    candidates: list[tuple[str, Path]] = []
    runtime_path = runtime_config_path()
    runtime_data = {}
    if runtime_path is not None:
        runtime_data = load_runtime(runtime_path)
        candidates.append(("runtime.json", runtime_path))

    if not runtime_data:
        for name in ("projects.json", ".env.local"):
            path = ROOT / "config" / name
            if regular_file(path):
                candidates.append((name.lstrip("."), path))

    for key, value in runtime_data.items():
        if key in CONFIG_KEYS or (key.startswith("TESTER_") and key.endswith("_FILE")):
            if value:
                candidates.append((Path(value).name or key.lower(), Path(value)))

    systemd_unit = Path("/etc/systemd/system/tester-control.service")
    if regular_file(systemd_unit):
        candidates.append(("tester-control.service", systemd_unit))
    drop_in_dir = Path("/etc/systemd/system/tester-control.service.d")
    if drop_in_dir.is_dir() and not drop_in_dir.is_symlink():
        candidates.extend((f"systemd-{path.name}", path) for path in sorted(drop_in_dir.iterdir()))
    legacy_cron = Path("/etc/cron.d/yeying-tester-full")
    if regular_file(legacy_cron):
        candidates.append((legacy_cron.name, legacy_cron))

    unique: list[tuple[str, Path]] = []
    seen: set[Path] = set()
    for label, path in candidates:
        if not regular_file(path):
            if path.exists() or path.is_symlink():
                raise ValueError(f"configuration source is not a regular file: {path}")
            continue
        resolved = path.resolve(strict=True)
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append((label, resolved))
    if not unique:
        raise ValueError("no Tester runtime configuration files were found")
    return unique


def runtime_backup_directory() -> str:
    runtime_path = runtime_config_path()
    if runtime_path is None:
        return ""
    runtime_data = load_runtime(runtime_path)
    value = runtime_data.get("TESTER_CONFIG_BACKUP_DIR", "")
    if not isinstance(value, str):
        raise ValueError("TESTER_CONFIG_BACKUP_DIR must be a string")
    return value


def backup() -> Path:
    configured_backup_root = runtime_backup_directory()
    backup_root = Path(
        os.environ.get("TESTER_CONFIG_BACKUP_DIR")
        or configured_backup_root
        or os.environ.get("TESTER_BACKUP_ROOT")
        or "/data/backups/tester/config"
    )
    backup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if backup_root.is_symlink() or not backup_root.is_dir():
        raise ValueError("Tester configuration backup directory is unsafe")
    os.chmod(backup_root, 0o700)
    lock_path = backup_root / ".config-backup.lock"
    if lock_path.is_symlink():
        raise ValueError("Tester configuration backup lock is unsafe")

    with lock_path.open("a", encoding="utf-8") as lock:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        final = backup_root / f"tester-config-{stamp}-{os.getpid()}"
        temporary = Path(tempfile.mkdtemp(prefix=".tester-config-", dir=backup_root))
        os.chmod(temporary, 0o700)
        try:
            files = []
            labels: set[str] = set()
            for label, source in configured_files():
                safe_label = re.sub(r"[^a-zA-Z0-9._-]", "_", label)
                safe_label = safe_label if safe_label not in ("", ".", "..") else "config"
                if safe_label in labels:
                    safe_label = f"{source.stem}-{hashlib.sha256(str(source).encode()).hexdigest()[:8]}{source.suffix}"
                labels.add(safe_label)
                destination = temporary / safe_label
                shutil.copyfile(source, destination)
                os.chmod(destination, 0o600)
                files.append({
                    "name": safe_label,
                    "source": str(source),
                    "sha256": digest(destination),
                })
            metadata = {
                "schema_version": 1,
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "files": files,
            }
            manifest = temporary / "manifest.json"
            manifest.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.chmod(manifest, 0o600)
            os.rename(temporary, final)
        except BaseException:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
    return final


if __name__ == "__main__":
    try:
        destination = backup()
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise SystemExit(f"Tester configuration backup failed: {error}") from error
    print(f"Tester configuration backup created: {destination}")

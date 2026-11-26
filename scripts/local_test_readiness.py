#!/usr/bin/env python3
"""Check that local-source E2E prerequisites are present before starting tests."""

import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "local-test-dependencies.json"
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


def read_env(path: Path | None) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path:
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def run_git(repo: Path, *args: str) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def repository_status(name: str, repo: Path) -> dict:
    if not repo.is_dir():
        return {"name": name, "status": "missing", "detail": "repository directory not found"}
    if not (repo / ".git").exists() and run_git(repo, "rev-parse", "--git-dir") is None:
        return {"name": name, "status": "invalid", "detail": "not a Git checkout"}
    remote_main = subprocess.run(
        ["git", "-C", str(repo), "ls-remote", "origin", "refs/heads/main"],
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    if remote_main.returncode != 0:
        return {"name": name, "status": "blocked", "detail": "could not read origin/main"}
    head = remote_main.stdout.split()[0] if remote_main.stdout.split() else None
    return {
        "name": name,
        "status": "ready" if head and len(head) == 40 else "blocked",
        "origin_main": head[:12] if head else None,
        "detail": "origin/main is fetchable; local branch and edits will be preserved" if head else "origin/main was not found",
    }


def local_endpoint(value: str) -> tuple[str, int] | None:
    try:
        parsed = urlparse(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return None
        if parsed.hostname.lower() not in LOOPBACK_HOSTS:
            return None
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        return parsed.hostname, port
    except ValueError:
        return None


def tcp_ready(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def select_projects(config: dict, requested: str) -> list[str]:
    if requested == "all":
        return list(config["full_run_projects"])
    if requested not in config["projects"]:
        raise ValueError(f"unknown Tester project: {requested}")
    return [requested]


def plan(config: dict, source_root: Path, projects: list[str], env: dict[str, str], require_services: bool, local_offset: int) -> dict:
    repository_names = sorted({
        repo
        for project in projects
        for repo in config["projects"][project]["repositories"]
    })
    service_names = sorted({
        service
        for project in projects
        for service in config["projects"][project]["services"]
    })
    repositories = [repository_status(name, source_root / name) for name in repository_names]
    services = []
    for name in service_names:
        definition = config["services"][name]
        url_key = definition.get("url_key")
        value = env.get(url_key, "") if url_key else ""
        if url_key and value:
            endpoint = local_endpoint(value)
            if endpoint is None:
                services.append({
                    "name": name,
                    "status": "blocked",
                    "detail": f"{url_key} must target a loopback URL",
                })
                continue
            host, port = endpoint
        else:
            host, port = "127.0.0.1", definition.get("port", definition.get("default_port"))
        if local_offset and definition.get("kind") != "middleware":
            port = int(port) + local_offset
        if not port:
            services.append({"name": name, "status": "blocked", "detail": "no local port configured"})
            continue
        listener_ready = tcp_ready(host, int(port))
        services.append({
            "name": name,
            "status": "ready" if listener_ready else "down",
            "detail": "TCP listener is reachable" if listener_ready else "TCP listener is unavailable",
        })
    blocked = any(item["status"] != "ready" for item in repositories)
    if require_services:
        blocked = blocked or any(item["status"] != "ready" for item in services)
    return {
        "mode": "local-source",
        "projects": projects,
        "source_root": str(source_root),
        "repositories": repositories,
        "services": services,
        "status": "blocked" if blocked else "ready",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", help="one Playwright project or 'all'")
    parser.add_argument("--source-root", type=Path, default=Path(os.environ.get("TESTER_SOURCE_ROOT", "/root/code")))
    parser.add_argument("--env-file", type=Path, default=None)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    parser.add_argument("--require-services", action="store_true", help="fail when required local listeners are down")
    parser.add_argument("--local-source", action="store_true", help="check the isolated local-source port range")
    args = parser.parse_args()
    try:
        config = json.loads(args.config.read_text(encoding="utf-8"))
        projects = select_projects(config, args.project)
        env_file = args.env_file
        if env_file is None:
            candidate = ROOT / ".env.local"
            env_file = candidate if candidate.exists() else None
        offset = int(os.environ.get("TESTER_LOCAL_PORT_OFFSET", "10000")) if args.local_source else 0
        result = plan(config, args.source_root, projects, read_env(env_file), args.require_services, offset)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"Local-source preflight failed: {error}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"Local-source preflight: {result['status']} ({', '.join(projects)})")
        for category in ("repositories", "services"):
            for item in result[category]:
                print(f"{item['status'].upper():7} {item['name']}: {item['detail']}")
    return 0 if result["status"] == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())

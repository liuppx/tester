#!/usr/bin/env python3
"""Run selected community services directly from source checkouts.

The manager never starts or resets middleware. It requires middleware to be
ready, materializes clean detached worktrees at each repository's origin/main,
starts product processes from those worktrees, and records exactly what it
started so the test runner can stop them afterwards.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
from urllib.parse import urlparse, urlunparse

from local_test_readiness import DEFAULT_CONFIG, LOOPBACK_HOSTS, read_env, select_projects


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUNTIME = Path(os.environ.get("TESTER_LOCAL_RUNTIME_DIR", "/data/test-results-private/control/local-source"))
DEFAULT_SOURCE_ROOT = Path(os.environ.get("TESTER_SOURCE_ROOT", "/root/code"))
DEFAULT_PORTS = {
    "NODE_BASE_URL": 8991, "NODE_API_URL": 8100,
    "ROUTER_BASE_URL": 3011, "ROUTER_WEB_URL": 5181,
    "WAREHOUSE_BASE_URL": 5173, "WAREHOUSE_WEBDAV_URL": 6065, "WAREHOUSE_ADMIN_URL": 6066,
    "CHAT_BASE_URL": 3020,
    "SOCIAL_BASE_URL": 8888, "SOCIAL_WEB_URL": 8082, "SOCIAL_IDENTITY_URL": 8901,
    "SOCIAL_SERVER_PORT": 8878, "SOCIAL_RTC_PORT": 8890,
    "PROJECT_BASE_URL": 2222,
    "KNOWLEDGE_API_URL": 8000, "KNOWLEDGE_BASE_URL": 5175,
    "ORCHESTRATOR_API_URL": 3900, "ORCHESTRATOR_BASE_URL": 5174,
}
MIDDLEWARE_NAMES = {"postgresql", "redis", "mysql", "mailpit"}
BUILD_CACHE_SCHEMA = "1"


def tcp_ready(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def private_yaml_copy(source: Path, target: Path, updates: dict[str, dict[str, str | int]]) -> None:
    """Change selected config keys while preserving all other private settings."""
    lines = source.read_text(encoding="utf-8").splitlines()
    for section, values in updates.items():
        header = next((index for index, line in enumerate(lines) if re.match(rf"^{re.escape(section)}:\s*(?:#.*)?$", line)), None)
        if header is None:
            raise RuntimeError(f"required local config section is missing: {section}")
        end = next((index for index in range(header + 1, len(lines)) if re.match(r"^\S[^:]*:\s*(?:#.*)?$", lines[index])), len(lines))
        insertion = []
        for key, value in values.items():
            match_at = next((index for index in range(header + 1, end) if re.match(rf"^\s+{re.escape(key)}:\s*", lines[index])), None)
            indent = "  "
            encoded = json.dumps(value) if isinstance(value, str) else str(value)
            rendered = f"{indent}{key}: {encoded}"
            if match_at is None:
                insertion.append(rendered)
            else:
                indent = lines[match_at][:len(lines[match_at]) - len(lines[match_at].lstrip())]
                lines[match_at] = f"{indent}{key}: {encoded}"
        lines[header + 1:header + 1] = insertion
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    target.chmod(0o600)


def replace_loopback_ports(source: Path, target: Path, ports: dict[str, int]) -> None:
    text = source.read_text(encoding="utf-8")
    for key, old in DEFAULT_PORTS.items():
        new = ports.get(key)
        if new and new != old:
            text = re.sub(rf"(localhost|127\.0\.0\.1):{old}(\b)", rf"127.0.0.1:{new}\2", text)
    target.write_text(text, encoding="utf-8")
    target.chmod(0o600)


def yaml_scalar(path: Path, section: str, key: str) -> str:
    active = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if re.match(rf"^{re.escape(section)}:\s*$", line):
            active = True
            continue
        if active and re.match(r"^\S[^:]*:\s*", line):
            break
        if active and re.match(rf"^\s+{re.escape(key)}:\s*", line):
            value = line.split(":", 1)[1].strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            return value
    raise RuntimeError(f"required local config value is missing: {section}.{key}")


def dsn_with_database(dsn: str, database: str) -> str:
    parsed = urlparse(dsn)
    if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname or not parsed.username:
        raise RuntimeError("Router local-source mode requires a PostgreSQL sql_dsn")
    if not re.fullmatch(r"tester_router_e2e_[a-f0-9]{8}", database):
        raise RuntimeError("invalid isolated Router database name")
    return urlunparse(parsed._replace(path="/" + database))


def create_router_test_database(source_config: Path, database: str) -> None:
    dsn = yaml_scalar(source_config, "database", "sql_dsn")
    owner = urlparse(dsn).username
    if not owner or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", owner):
        raise RuntimeError("Router database owner is not a safe PostgreSQL role name")
    sql = f'CREATE DATABASE "{database}" OWNER "{owner}"'
    result = subprocess.run(
        ["docker", "exec", "postgres", "sh", "-c", 'psql -U "$POSTGRES_USER" -d postgres -v ON_ERROR_STOP=1 -c "$1"', "sh", sql],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if result.returncode:
        raise RuntimeError("could not create isolated Router E2E database; check the PostgreSQL owner role and permissions")


def drop_router_test_database(database: str) -> None:
    if not re.fullmatch(r"tester_router_e2e_[a-f0-9]{8}", database):
        return
    sql = f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)'
    subprocess.run(
        ["docker", "exec", "postgres", "sh", "-c", 'psql -U "$POSTGRES_USER" -d postgres -v ON_ERROR_STOP=1 -c "$1"', "sh", sql],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=30,
    )


def endpoint_port(value: str | None, key: str) -> int:
    if not value:
        port = DEFAULT_PORTS[key]
    else:
        parsed = urlparse(value)
        if parsed.hostname not in LOOPBACK_HOSTS or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise RuntimeError(f"{key} must target a loopback URL in local-source mode")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    offset = int(os.environ.get("TESTER_LOCAL_PORT_OFFSET", "10000"))
    if offset < 0 or port + offset > 65535:
        raise RuntimeError("TESTER_LOCAL_PORT_OFFSET produces an invalid port")
    return port + offset


def git(repo: Path, *args: str) -> str | None:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def source_checkout(project: str, source_root: Path, worktree_root: Path, sha: str, ports: dict[str, int], database_names: dict[str, str]) -> Path:
    """Materialize one already-resolved origin/main commit in a managed worktree."""
    original = source_root / project
    if not original.is_dir() or git(original, "rev-parse", "--git-dir") is None:
        raise RuntimeError(f"source Git checkout is missing: {original}")
    worktree = worktree_root / project / sha
    marker = worktree / ".tester-source-checkout"
    if worktree.exists():
        if not marker.is_file() or marker.read_text(encoding="utf-8").strip() != sha:
            raise RuntimeError(f"refusing unmanaged local source directory: {worktree}")
        if git(worktree, "rev-parse", "HEAD") != sha:
            raise RuntimeError(f"managed source directory has the wrong commit: {worktree}")
        reset_environmental_files(project, worktree)
        overlay_runtime_configuration(project, original, worktree, ports, database_names)
        return worktree
    worktree.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    subprocess.run(["git", "-C", str(original), "worktree", "add", "--detach", str(worktree), sha], check=True)
    marker.write_text(sha + "\n", encoding="utf-8")
    marker.chmod(0o600)
    overlay_runtime_configuration(project, original, worktree, ports, database_names)
    return worktree


def reset_environmental_files(project: str, worktree: Path) -> None:
    paths = {
        "node": ("src/server.ts", "web/vite.config.ts"),
        "router": ("web/vite.config.mjs",),
        "warehouse": ("web/vite.config.ts",),
        "social": ("cmd", "web/vue.config.js", "web/.env.development"),
    }.get(project, ())
    tracked = [relative for relative in paths if (worktree / relative).is_file()]
    if tracked:
        subprocess.run(["git", "-C", str(worktree), "checkout", "--", *tracked], check=True)


def overlay_runtime_configuration(project: str, original: Path, worktree: Path, ports: dict[str, int], database_names: dict[str, str]) -> None:
    # Runtime configuration is ignored by Git and stays in the original checkout.
    config_files = {
        "node": ("run/secrets.enc.json", "run/.secrets-password"),
        "chat": (".env",),
        "warehouse": (".env",),
        "project": (".env",),
        "knowledge": (".env",),
    }.get(project, ())
    for relative in config_files:
        source, target = original / relative, worktree / relative
        if source.is_file() and (relative == ".env" or not target.exists()):
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if project in {"chat", "warehouse", "project", "knowledge"} and relative == ".env":
                target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
                target.chmod(0o600)
            else:
                target.symlink_to(source)
    if project == "node" and (original / "config.js").is_file():
        (worktree / "config.js").write_text(
            "const source = require(" + json.dumps(str(original / "config.js")) + ");\n"
            "const origins = [" + json.dumps(f"http://localhost:{ports['NODE_BASE_URL']}") + ", " + json.dumps(f"http://127.0.0.1:{ports['NODE_BASE_URL']}") + "];\n"
            "module.exports = { ...source, app: { ...source.app, port: " + str(ports["NODE_API_URL"]) + ", corsAllowedOrigins: [...new Set([...(source.app?.corsAllowedOrigins || []), ...origins])] } };\n",
            encoding="utf-8",
        )
        (worktree / "config.js").chmod(0o600)
    if project == "node":
        server_source = worktree / "src/server.ts"
        if server_source.is_file():
            text = server_source.read_text(encoding="utf-8")
            updated, count = re.subn(r"app\.listen\(port,\s*['\"]0\.0\.0\.0['\"]", "app.listen(port, '127.0.0.1'", text, count=1)
            if count != 1:
                raise RuntimeError("Node source server bind signature changed; refusing an unisolated local run")
            server_source.write_text(updated, encoding="utf-8")
    if project == "router" and (original / "config.yaml").is_file():
        updates = {"server": {"address": "127.0.0.1", "port": ports["ROUTER_BASE_URL"]}}
        if database_names.get("router"):
            dsn = yaml_scalar(original / "config.yaml", "database", "sql_dsn")
            updates["database"] = {"sql_dsn": dsn_with_database(dsn, database_names["router"])}
        private_yaml_copy(original / "config.yaml", worktree / "config.yaml", updates)
        replace_loopback_ports(worktree / "config.yaml", worktree / "config.yaml", ports)
    if project == "warehouse" and (original / "config.yaml").is_file():
        private_yaml_copy(original / "config.yaml", worktree / "config.yaml", {
            "server": {"address": "127.0.0.1", "port": ports["WAREHOUSE_WEBDAV_URL"]},
            "s3": {"address": "127.0.0.1", "port": ports["WAREHOUSE_ADMIN_URL"]},
            "webdav": {"directory": str(worktree / "data")},
        })
    if project in {"chat", "project", "knowledge"} and (worktree / ".env").is_file():
        replace_loopback_ports(worktree / ".env", worktree / ".env", ports)
    if project in {"node", "router", "warehouse", "social"}:
        web = worktree / "web"
        for pattern in ("vite.config.*", "vue.config.js"):
            for path in web.glob(pattern):
                if path.is_file():
                    replace_loopback_ports(path, path, ports)
    if project == "project" and (original / "vendor").is_dir() and not (worktree / "vendor").exists():
        (worktree / "vendor").symlink_to(original / "vendor", target_is_directory=True)
    if project == "social" and (original / "config").is_dir():
        shutil.rmtree(worktree / "config", ignore_errors=True)
        shutil.copytree(original / "config", worktree / "config", symlinks=False)
    if project == "social" and (worktree / "config").is_dir():
        service_ports = {"platform": ports["SOCIAL_BASE_URL"], "server": ports["SOCIAL_SERVER_PORT"], "rtc": ports["SOCIAL_RTC_PORT"], "web3-identity": ports["SOCIAL_IDENTITY_URL"]}
        for service, port in service_ports.items():
            path = worktree / "config" / service / "application.yml"
            if path.is_file():
                private_yaml_copy(path, path, {"server": {"address": "127.0.0.1", "port": port}})
                replace_loopback_ports(path, path, ports)
        web_config = worktree / "web/vue.config.js"
        if web_config.is_file():
            replace_loopback_ports(web_config, web_config, ports)
        development_env = worktree / "web/.env.development"
        if development_env.is_file():
            replace_loopback_ports(development_env, development_env, ports)
            development_env.chmod(0o600)
def service_env(overrides: dict[str, str], extra: dict[str, str] | None = None) -> dict[str, str]:
    inherited = {
        "PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TZ", "TMPDIR",
        "JAVA_HOME", "NVM_DIR", "GOPATH", "GOMODCACHE", "UV_CACHE_DIR",
    }
    env = {key: value for key, value in os.environ.items() if key in inherited}
    env.update(overrides)
    if extra:
        env.update(extra)
    env.setdefault("PATH", "/opt/node-v24/bin:/usr/local/go/bin:/usr/local/bin:/usr/bin:/bin")
    env.setdefault("NPM_CONFIG_REGISTRY", "https://registry.npmmirror.com")
    env.setdefault("GOPROXY", "https://goproxy.cn,direct")
    env.setdefault("UV_INDEX_URL", "https://pypi.tuna.tsinghua.edu.cn/simple")
    env.setdefault("PIP_INDEX_URL", "https://pypi.tuna.tsinghua.edu.cn/simple")
    return env


def configure_maven_mirror(runtime: Path, env: dict[str, str]) -> None:
    settings = runtime / "configs/maven-settings.xml"
    settings.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    settings.write_text(
        "<settings xmlns=\"http://maven.apache.org/SETTINGS/1.2.0\">"
        "<mirrors><mirror><id>aliyun-public</id><mirrorOf>*</mirrorOf>"
        "<name>Aliyun Maven mirror</name>"
        "<url>https://maven.aliyun.com/repository/public</url>"
        "</mirror></mirrors></settings>\n",
        encoding="utf-8",
    )
    settings.chmod(0o600)
    env.setdefault("MAVEN_ARGS", f"--settings {settings}")


def command_for(name: str, sources: dict[str, Path], ports: dict[str, int]) -> tuple[Path, list[str], dict[str, str]] | None:
    repo = lambda project: sources[project]
    if name == "node-api":
        return repo("node"), ["scripts/starter.sh", "start"], {"APP_PORT": str(ports["NODE_API_URL"]), "RUN_DIR": str(repo("node") / "run"), "LOG_DIR": str(repo("node") / "logs")}
    if name == "node-web":
        return repo("node") / "web", ["npm", "run", "dev", "--", "--host", "127.0.0.1", "--port", str(ports["NODE_BASE_URL"])], {"VITE_DEV_API_PROXY_TARGET": f"http://127.0.0.1:{ports['NODE_API_URL']}", "VITE_NODE_API_ENDPOINT": f"http://127.0.0.1:{ports['NODE_API_URL']}"}
    if name == "router-api":
        return repo("router"), ["build/router", "--config", str(repo("router") / "config.yaml"), "--port", str(ports["ROUTER_BASE_URL"])], {}
    if name == "router-web":
        return repo("router") / "web", ["npm", "run", "dev", "--", "--host", "127.0.0.1", "--port", str(ports["ROUTER_WEB_URL"])], {"VITE_SERVER": f"http://127.0.0.1:{ports['ROUTER_BASE_URL']}"}
    if name in {"warehouse-api", "warehouse-webdav", "warehouse-admin"}:
        return repo("warehouse"), ["bin/warehouse", "--config", str(repo("warehouse") / "config.yaml")], {}
    if name == "warehouse-web":
        return repo("warehouse") / "web", ["npm", "run", "dev", "--", "--host", "127.0.0.1", "--port", str(ports["WAREHOUSE_BASE_URL"])], {"VITE_API_BASE": f"http://127.0.0.1:{ports['WAREHOUSE_WEBDAV_URL']}"}
    if name == "chat-web":
        return repo("chat"), ["npm", "run", "dev", "--", "--hostname", "127.0.0.1", "--port", str(ports["CHAT_BASE_URL"])], {}
    if name in {"social-platform", "social-server", "social-rtc", "social-identity"}:
        return repo("social"), ["./cmd", "local-start", "platform", "server", "rtc", "web3-identity"], {}
    if name == "social-web":
        return repo("social") / "web", ["npm", "run", "serve", "--", "--host", "127.0.0.1", "--port", str(ports["SOCIAL_WEB_URL"])], {}
    if name == "project-api":
        return repo("project"), ["scripts/starter.sh", "start"], {"LARAVELS_LISTEN_IP": "127.0.0.1", "LARAVELS_LISTEN_PORT": str(ports["PROJECT_BASE_URL"])}
    if name == "knowledge-api":
        return repo("knowledge"), ["uv", "run", "uvicorn", "knowledge.main:app", "--host", "127.0.0.1", "--port", str(ports["KNOWLEDGE_API_URL"])], {}
    if name == "knowledge-web":
        return repo("knowledge") / "web", ["npm", "run", "dev", "--", "--host", "127.0.0.1", "--port", str(ports["KNOWLEDGE_BASE_URL"])], {}
    if name == "orchestrator-api":
        return repo("orchestrator") / "backend", ["uv", "run", "orchestrator"], {"PORT": str(ports["ORCHESTRATOR_API_URL"])}
    if name == "orchestrator-web":
        return repo("orchestrator") / "web", ["npm", "run", "dev", "--", "--host", "127.0.0.1", "--port", str(ports["ORCHESTRATOR_BASE_URL"])], {}
    return None


def ensure_npm_dependencies(project: str, repo: Path, package_dir: Path, runtime: Path, env: dict[str, str], logs: Path) -> None:
    package_json = package_dir / "package.json"
    if not package_json.is_file():
        return
    lock = package_dir / "package-lock.json"
    lock_hash = hashlib.sha256(lock.read_bytes()).hexdigest() if lock.is_file() else "manifest"
    install_root = runtime / "dependency-stamps"
    relative = str(package_dir.relative_to(repo)).replace("/", "-") if package_dir != repo else "root"
    marker = install_root / f"{project}-{relative}-{lock_hash}.json"
    node_modules = package_dir / "node_modules"
    if marker.exists() and node_modules.is_dir():
        return
    command = ["npm", "ci", "--no-audit", "--no-fund"] if lock.is_file() else ["npm", "install", "--no-audit", "--no-fund", "--package-lock=false"]
    process_env = dict(env)
    process_env.setdefault("NPM_CONFIG_REGISTRY", "https://registry.npmmirror.com")
    if not lock.is_file():
        # Some older source manifests expose peer graphs that npm 10 cannot
        # resolve strictly; use npm's documented compatibility mode only when
        # there is no committed lockfile to define an exact peer tree.
        process_env.setdefault("NPM_CONFIG_LEGACY_PEER_DEPS", "true")
    log_path = logs / f"npm-{project}-{package_dir.name}.log"
    logs.mkdir(parents=True, exist_ok=True, mode=0o700)
    with log_path.open("w", encoding="utf-8") as stream:
        subprocess.run(command, cwd=package_dir, env=process_env, stdout=stream, stderr=subprocess.STDOUT, check=True)
    marker.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    marker.write_text(json.dumps({"project": project, "lock_sha256": lock_hash}) + "\n", encoding="utf-8")


def prepare_dependencies(project: str, repo: Path, services: list[str], runtime: Path, env: dict[str, str], logs: Path) -> None:
    package_dirs = {
        "node": [repo, repo / "web"],
        "router": [repo / "web"],
        "warehouse": [repo / "web"],
        "chat": [repo],
        "social": [repo / "web"],
        "project": [repo],
        "knowledge": [repo / "web"],
    }
    project_services = {
        "node": {"node-api", "node-web"},
        "router": {"router-api", "router-web"},
        "warehouse": {"warehouse-api", "warehouse-webdav", "warehouse-admin", "warehouse-web"},
        "chat": {"chat-web"},
        "social": {"social-platform", "social-server", "social-rtc", "social-identity", "social-web"},
        "project": {"project-api"},
        "knowledge": {"knowledge-api", "knowledge-web"},
    }
    if not (set(services) & project_services.get(project, set())):
        return
    for package_dir in package_dirs.get(project, []):
        ensure_npm_dependencies(project, repo, package_dir, runtime, env, logs)


def build_for(project: str, repo: Path, stamps: Path, env: dict[str, str]) -> None:
    sha = git(repo, "rev-parse", "HEAD") or "unknown"
    stamp = stamps / f"{project}-{sha}-local-{BUILD_CACHE_SCHEMA}"
    if stamp.exists():
        return
    commands = {
        "router": ["go", "build", "-buildvcs=false", "-o", "build/router", "./cmd/router"],
        "warehouse": ["go", "build", "-buildvcs=false", "-o", "bin/warehouse", "./cmd/warehouse"],
        "node": ["npm", "run", "build"],
    }
    command = commands.get(project)
    if command and project == "node":
        npm_env = dict(env)
        npm_env["NPM_CONFIG_REGISTRY"] = "https://registry.npmmirror.com"
        subprocess.run(command, cwd=repo, env=npm_env, check=True)
    elif command:
        subprocess.run(command, cwd=repo, env=env, check=True)
    stamps.mkdir(parents=True, exist_ok=True)
    stamp.write_text("built\n", encoding="utf-8")


def wait_port(url: str, timeout: float = 60) -> None:
    parsed = urlparse(url)
    if parsed.hostname not in LOOPBACK_HOSTS or not parsed.port:
        return
    deadline = time.time() + timeout
    while time.time() < deadline:
        if tcp_ready(parsed.hostname, parsed.port):
            return
        time.sleep(1)
    raise RuntimeError(f"service did not open {parsed.hostname}:{parsed.port} within {int(timeout)}s")


def start(args: argparse.Namespace) -> int:
    config = json.loads(args.config.read_text(encoding="utf-8"))
    projects = select_projects(config, args.project)
    base = read_env(args.env_file)
    ports = {key: endpoint_port(base.get(key), key) for key in DEFAULT_PORTS}
    services = list(dict.fromkeys(service for name in projects for service in config["projects"][name]["services"]))
    database_names = {"router": f"tester_router_e2e_{secrets.token_hex(4)}"} if "router-api" in services else {}
    source_root = args.source_root
    worktree_root = args.runtime / "worktrees"
    source_projects = sorted({repo for name in projects for repo in config["projects"][name]["repositories"]})
    missing = [project for project in source_projects if not (source_root / project).is_dir()]
    if missing:
        raise RuntimeError("missing source repositories: " + ", ".join(missing))
    sources: dict[str, Path] = {}
    shas: dict[str, str] = {}
    # Resolve all main commits first so a missing repository or remote branch
    # cannot leave a partially materialized full-run environment.
    fetched: dict[str, str] = {}
    for project in source_projects:
        original = source_root / project
        subprocess.run(["git", "-C", str(original), "fetch", "--no-tags", "origin", "main"], check=True)
        sha = git(original, "rev-parse", "FETCH_HEAD")
        if not sha or len(sha) != 40:
            raise RuntimeError(f"could not resolve origin/main for {project}")
        fetched[project] = sha
    for project in source_projects:
        sources[project] = source_checkout(project, source_root, worktree_root, fetched[project], ports, database_names)
        shas[project] = fetched[project]

    overrides = {key: f"http://127.0.0.1:{value}" for key, value in ports.items()}
    overrides["TEST_ENV"] = "local"
    if "books" in sources:
        overrides["BOOKS_REPO_PATH"] = str(sources["books"])
    if "marketplace" in sources:
        overrides["MARKETPLACE_REPO_PATH"] = str(sources["marketplace"])
    if "wallet" in sources:
        overrides["WALLET_REPO_PATH"] = str(sources["wallet"])
        overrides["WALLET_EXTENSION_PATH"] = str(sources["wallet"])
    args.runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    logs = args.runtime / "logs"
    logs.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamps = args.runtime / "build-stamps"
    order = {name: index for index, name in enumerate((
        "postgresql", "redis", "mysql", "mailpit",
        "node-api", "node-web", "router-api", "router-web",
        "warehouse-api", "warehouse-webdav", "warehouse-admin", "warehouse-web",
        "chat-web", "social-platform", "social-server", "social-rtc", "social-identity", "social-web",
        "project-api", "knowledge-api", "knowledge-web", "orchestrator-api", "orchestrator-web",
    ))}
    services.sort(key=lambda name: order.get(name, 999))
    middleware_names = {"postgresql", "redis", "mysql", "mailpit"}
    for name in services:
        if name not in middleware_names:
            continue
        middleware_port = config["services"][name]["port"]
        if not tcp_ready("127.0.0.1", middleware_port):
            raise RuntimeError(f"middleware {name} is unavailable on local port {middleware_port}; start it first")
    if "project-api" in services and os.environ.get("TESTER_LOCAL_ALLOW_DB_MIGRATIONS") != "1":
        raise RuntimeError("Project local startup applies database migrations; set TESTER_LOCAL_ALLOW_DB_MIGRATIONS=1 only after verifying its database is disposable")
    for name in services:
        if name in middleware_names:
            continue
        definition = config["services"].get(name, {})
        url_key = definition.get("url_key")
        port = ports.get(url_key) if url_key else definition.get("port")
        if not url_key and definition.get("kind") == "product-service":
            port = int(port) + int(os.environ.get("TESTER_LOCAL_PORT_OFFSET", "10000"))
        if port and tcp_ready("127.0.0.1", int(port)):
            raise RuntimeError(f"local port {port} is already in use; stop the existing service before local-source testing")
    started: list[dict] = []
    started_groups: set[str] = set()
    env = service_env(overrides)
    configure_maven_mirror(args.runtime, env)
    for project, repo in sources.items():
        prepare_dependencies(project, repo, services, args.runtime, env, logs)
    local_databases: list[dict[str, str]] = []
    try:
        if database_names.get("router"):
            create_router_test_database(sources["router"] / "config.yaml", database_names["router"])
            local_databases.append({"engine": "postgres", "name": database_names["router"]})
        for name in services:
            if name in middleware_names:
                continue
            group = name.split("-", 1)[0]
            if group in {"warehouse", "social"} and group in started_groups:
                continue
            info = command_for(name, sources, ports)
            if info is None:
                raise RuntimeError(f"no local-source adapter for {name}")
            cwd, command, extra_env = info
            if not cwd.is_dir():
                raise RuntimeError(f"source directory is missing for {name}: {cwd}")
            project = next((item for item in ("node", "router", "warehouse", "chat", "social", "project", "knowledge", "orchestrator") if item in cwd.parts), None)
            if project:
                build_for(project, sources[project], stamps, env)
            url_key = config["services"].get(name, {}).get("url_key")
            log_path = logs / f"{name}.log"
            stream = log_path.open("a", encoding="utf-8")
            process = subprocess.Popen(command, cwd=cwd, env=service_env(overrides, extra_env), stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
            stream.close()
            started.append({"name": name, "pid": process.pid, "cwd": str(cwd), "command": command, "log": str(log_path)})
            started_groups.add(group)
            if url_key:
                wait_port(overrides[url_key])
        for name in services:
            if name in middleware_names:
                continue
            definition = config["services"].get(name, {})
            url_key = definition.get("url_key")
            if url_key:
                endpoint = urlparse(overrides[url_key])
                port = endpoint.port
            else:
                port = definition.get("port")
            if not url_key and definition.get("kind") == "product-service":
                port = int(port) + int(os.environ.get("TESTER_LOCAL_PORT_OFFSET", "10000"))
            if port and not tcp_ready("127.0.0.1", int(port)):
                raise RuntimeError(f"local service {name} is not ready on port {port}")
    except Exception:
        for item in reversed(started):
            stop_item(item)
        for database in reversed(local_databases):
            if database["engine"] == "postgres":
                drop_router_test_database(database["name"])
        raise
    args.state.write_text(json.dumps({"started": started, "env": overrides, "projects": projects, "local_databases": local_databases}, indent=2) + "\n", encoding="utf-8")
    source_commits = {project: sha for project, sha in shas.items()}
    manifest = {
        "projects": projects,
        "source_commits": source_commits,
        "runtime_overrides": {
            "network_bind": "loopback",
            "ports": "isolated local-source port range",
            "config": "private runtime copies per source worktree",
        },
    }
    args.manifest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    args.manifest.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    exports = {**overrides, "TESTER_LOCAL_SOURCE_MANIFEST": str(args.manifest)}
    args.env_output.write_text("".join(f"export {key}={shlex.quote(value)}\n" for key, value in exports.items()), encoding="utf-8")
    print(f"Started {len(started)} local source services for {', '.join(projects)}")
    return 0


def stop(args: argparse.Namespace) -> int:
    if not args.state.exists():
        return 0
    state = json.loads(args.state.read_text(encoding="utf-8"))
    for item in reversed(state.get("started", [])):
        stop_item(item)
    for database in reversed(state.get("local_databases", [])):
        if database.get("engine") == "postgres":
            drop_router_test_database(database.get("name", ""))
    args.state.unlink(missing_ok=True)
    args.env_output.unlink(missing_ok=True)
    return 0


def stop_item(item: dict) -> None:
    cwd = Path(item["cwd"])
    command = item.get("command", [])
    if command and command[0] == "scripts/starter.sh":
        subprocess.run(["scripts/starter.sh", "stop"], cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    elif command and command[0] == "./cmd":
        subprocess.run(["./cmd", "local-stop"], cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    else:
        try:
            os.killpg(item["pid"], signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                os.kill(item["pid"], 0)
            except ProcessLookupError:
                break
            except PermissionError:
                pass
            time.sleep(0.2)
        try:
            os.killpg(item["pid"], signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "stop"))
    parser.add_argument("project", help="one project or all")
    parser.add_argument("--source-root", type=Path, default=DEFAULT_SOURCE_ROOT)
    parser.add_argument("--env-file", type=Path, default=None)
    parser.add_argument("--runtime", type=Path, default=DEFAULT_RUNTIME)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--env-output", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    args = parser.parse_args()
    args.state = args.state or args.runtime / "state.json"
    args.env_output = args.env_output or args.runtime / "env.sh"
    args.manifest = args.manifest or args.runtime / "source-manifest.json"
    if args.env_file is None:
        candidate = ROOT / ".env.local"
        args.env_file = candidate if candidate.exists() else None
    try:
        return start(args) if args.action == "start" else stop(args)
    except (OSError, RuntimeError, subprocess.CalledProcessError, json.JSONDecodeError) as error:
        print(f"Local-source service manager failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

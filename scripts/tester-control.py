#!/usr/bin/env python3
"""Queue per-project Tester runs and watch upstream main branches on the test host."""

from __future__ import annotations

from datetime import date, datetime, time as clock_time, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from http.cookies import SimpleCookie
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from urllib.parse import parse_qs, unquote, urlsplit

import tester_analysis as analysis


REPO = Path(__file__).resolve().parents[1]
CONFIG = Path(os.environ.get("TESTER_CONTROL_CONFIG", REPO / "scripts/tester-control-projects.json"))
PRIVATE = Path(os.environ.get("TESTER_PRIVATE_REPORT_DIR", "/data/test-results-private")) / "control"
PUBLIC = Path(os.environ.get("TESTER_REPORT_DIR", "/data/test-results"))
STATE_FILE = PRIVATE / "state.json"
LOG_DIR = PRIVATE / "logs"
RUNNER = REPO / "scripts/deploy-and-test-project.sh"
FULL_RUNNER = REPO / "scripts/run-full-on-test-machine.sh"
TESTER_MAIN_RUNNER = REPO / "scripts/test-tester-main.sh"
FULL = "__full__"
MULTI_PROJECT = "__batch__"
TESTER_MAIN_KEY = "__tester_main__"
PROJECT_ALIASES = {"books": "book"}
DEPLOYMENT_ADAPTERS = {"book", "chat", "marketplace", "router", "wallet"}
ORIGIN = os.environ.get("TESTER_CONTROL_ORIGIN", "https://tester.tidukongjian.com")
AUTH_DATABASE = Path(os.environ.get("TESTER_AUTH_DATABASE", str(PRIVATE / "tester-auth.sqlite3")))
ADMIN_DID = os.environ.get("TESTER_ADMIN_DID", "").strip()
IDENTITY_ISSUER_URL = os.environ.get("TESTER_IDENTITY_ISSUER_URL", "https://node.yeying.pub").rstrip("/")
LOGIN_TTL_SECONDS = 300
SESSION_TTL_SECONDS = 8 * 60 * 60
SESSION_COOKIE = "tester_session"
LOGIN_SESSIONS: dict[str, dict] = {}
AUTH_SESSIONS: dict[str, dict] = {}
AUTH_MUTEX = threading.RLock()
POLL_SECONDS = int(os.environ.get("TESTER_MAIN_POLL_SECONDS", "300"))
RETRY_SECONDS = int(os.environ.get("TESTER_PROJECT_RETRY_SECONDS", "300"))
ROUTER_COOLDOWN_SECONDS = int(os.environ.get("TESTER_ROUTER_COOLDOWN_SECONDS", "1260"))
MUTEX = threading.RLock()
ARTIFACT_MODE = os.environ.get("TESTER_ARTIFACT_MODE") == "1"
LOCAL_SOURCE_MODE = os.environ.get("TESTER_LOCAL_SOURCE_MODE", "false").lower() in ("1", "true", "yes")
ALLOW_ARTIFACT_SOURCE_DEPLOY = os.environ.get("TESTER_ALLOW_PRODUCT_SOURCE_DEPLOY", "false").lower() in ("1", "true", "yes")
WATCH_UPSTREAM_MAIN = os.environ.get("TESTER_WATCH_UPSTREAM_MAIN", "1").lower() not in ("0", "false", "no")


def daily_schedule(env: dict | None = None) -> tuple[clock_time, ZoneInfo] | None:
    values = os.environ if env is None else env
    enabled = values.get("TESTER_DAILY_RUN_ENABLED", "false").lower()
    if enabled not in ("true", "false", "1", "0"):
        raise ValueError("TESTER_DAILY_RUN_ENABLED must be true or false")
    if enabled in ("false", "0"):
        return None
    raw_time = values.get("TESTER_DAILY_RUN_TIME", "03:00")
    try:
        hour, minute = map(int, raw_time.split(":"))
        run_at = clock_time(hour, minute)
    except (ValueError, TypeError) as error:
        raise ValueError("TESTER_DAILY_RUN_TIME must use HH:MM") from error
    if raw_time != f"{hour:02d}:{minute:02d}":
        raise ValueError("TESTER_DAILY_RUN_TIME must use zero-padded HH:MM")
    try:
        zone = ZoneInfo(values.get("TESTER_DAILY_RUN_TIMEZONE", "Asia/Shanghai"))
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ValueError("TESTER_DAILY_RUN_TIMEZONE is not a known timezone") from error
    return run_at, zone


def next_daily_run(now_at: datetime, run_at: clock_time, zone: ZoneInfo) -> datetime:
    local_now = now_at.astimezone(zone)
    candidate = datetime.combine(local_now.date(), run_at, tzinfo=zone)
    if candidate <= local_now:
        candidate = datetime.combine(local_now.date() + timedelta(days=1), run_at, tzinfo=zone)
    return candidate


def next_scheduled_daily_run(now_at: datetime, run_at: clock_time, zone: ZoneInfo,
                             scheduled_date: str | None) -> datetime:
    local_now = now_at.astimezone(zone)
    today = local_now.date()
    today_target = datetime.combine(today, run_at, tzinfo=zone)
    if today_target <= local_now and (scheduled_date is None or scheduled_date < today.isoformat()):
        return today_target
    return next_daily_run(now_at, run_at, zone)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_config() -> dict[str, dict]:
    raw = json.loads(CONFIG.read_text(encoding="utf-8"))
    configured = raw.get("projects")
    if not isinstance(configured, dict) or not configured:
        raise ValueError("No Tester projects configured")
    projects = {}
    for configured_name, value in configured.items():
        name = PROJECT_ALIASES.get(configured_name, configured_name)
        if not name.replace("-", "").isalnum() or not isinstance(value, dict):
            raise ValueError("Invalid project configuration")
        if name in projects:
            raise ValueError(f"Duplicate project configuration: {name}")
        repo = Path(value.get("repo", ""))
        if not repo.is_absolute():
            raise ValueError(f"Project repository path must be absolute: {name}")
        if value.get("enabled", False) and name not in DEPLOYMENT_ADAPTERS and not LOCAL_SOURCE_MODE:
            value = {**value, "enabled": False,
                     "reason": "Tester does not have a verified source-update adapter for this project."}
        elif value.get("enabled", False) and not (repo / ".git").exists():
            value = {**value, "enabled": False,
                     "reason": "Configured main checkout is missing; update its repository path before enabling the watcher."}
        projects[name] = value
    return projects


PROJECTS = load_config()


def save_state(state: dict) -> None:
    PRIVATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".state.", dir=PRIVATE)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(state, output, ensure_ascii=False, indent=2)
            output.write("\n")
        os.replace(temporary, STATE_FILE)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_state() -> dict:
    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    else:
        state = {"seen": {}, "pending": [], "active": None, "recent": [], "poll_errors": {}}
    if state.get("active"):
        state["pending"].insert(0, state["active"])
        state["active"] = None
    state["seen"] = {PROJECT_ALIASES.get(name, name): sha for name, sha in state.get("seen", {}).items()}
    scheduled_date = state.get("daily_scheduled_date")
    try:
        if scheduled_date is not None:
            date.fromisoformat(scheduled_date)
    except (TypeError, ValueError):
        scheduled_date = None
    state["daily_scheduled_date"] = scheduled_date
    for item in state.get("pending", []):
        normalize_batch_item(item)
    for item in state.get("recent", []):
        normalize_batch_item(item)
    if state.get("pending") or state.get("recent"):
        save_state(state)
    return state


def open_auth_database() -> sqlite3.Connection:
    AUTH_DATABASE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if AUTH_DATABASE.parent.is_symlink() or not AUTH_DATABASE.parent.is_dir():
        raise ValueError("Tester auth database directory is unsafe")
    os.chmod(AUTH_DATABASE.parent, 0o700)
    if AUTH_DATABASE.is_symlink():
        raise ValueError("Tester auth database must not be a symlink")
    if not AUTH_DATABASE.exists():
        descriptor = os.open(AUTH_DATABASE, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
    if not AUTH_DATABASE.is_file():
        raise ValueError("Tester auth database is not a regular file")
    os.chmod(AUTH_DATABASE, 0o600)
    connection = sqlite3.connect(AUTH_DATABASE, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=15000")
    return connection


def migrate_auth_database() -> None:
    migrations = sorted((REPO / "scripts/migrations").glob("[0-9][0-9][0-9][0-9]_*.sql"))
    with open_auth_database() as database:
        database.execute("PRAGMA journal_mode=WAL")
        database.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)")
        database.commit()
        applied = {row[0] for row in database.execute("SELECT version FROM schema_migrations")}
        for migration in migrations:
            version = int(migration.name.split("_", 1)[0])
            if version in applied:
                continue
            statements = [part.strip() for part in migration.read_text(encoding="utf-8").split(";") if part.strip()]
            database.execute("BEGIN IMMEDIATE")
            try:
                for statement in statements:
                    database.execute(statement)
                database.execute(
                    "INSERT INTO schema_migrations(version,name,applied_at) VALUES(?,?,?)",
                    (version, migration.name, now()),
                )
                database.commit()
            except BaseException:
                database.rollback()
                raise
    os.chmod(AUTH_DATABASE, 0o600)


def bootstrap_admin() -> None:
    if not ADMIN_DID:
        return
    if not ADMIN_DID.startswith("did:yeying:wid_") or len(ADMIN_DID) < 35:
        raise ValueError("TESTER_ADMIN_DID is invalid")
    with open_auth_database() as database:
        database.execute("UPDATE tester_users SET role='member',status='disabled' WHERE role='admin' AND did<>?", (ADMIN_DID,))
        database.execute(
            """INSERT INTO tester_users(did, identity_document, display_name, role, status, requested_at, approved_at, approved_by)
               VALUES(?, NULL, 'Tester administrator', 'admin', 'active', ?, ?, 'system')
               ON CONFLICT(did) DO UPDATE SET role='admin', status='active'""",
            (ADMIN_DID, now(), now()),
        )


def find_user(did: str) -> dict | None:
    with open_auth_database() as database:
        row = database.execute("SELECT * FROM tester_users WHERE did=?", (did,)).fetchone()
    return dict(row) if row else None


def list_users() -> list[dict]:
    with open_auth_database() as database:
        rows = database.execute(
            "SELECT did,display_name,role,status,requested_at,approved_at,approved_by,last_login_at FROM tester_users ORDER BY CASE status WHEN 'pending' THEN 0 WHEN 'active' THEN 1 ELSE 2 END, requested_at"
        ).fetchall()
    return [dict(row) for row in rows]


def verify_wallet_presentation(presentation: dict, audience: str, nonce: str,
                               trusted_document: dict | None = None) -> str:
    """Verify wallet proofs with Node's built-in Ed25519 implementation."""
    node = os.environ.get("TESTER_NODE_BIN", "node")
    if not os.path.isabs(node):
        node = str(REPO / "node_modules/.bin/node") if (REPO / "node_modules/.bin/node").is_file() else node
    result = subprocess.run(
        [node, str(REPO / "scripts/verify-wallet-presentation.mjs")],
        input=json.dumps({
            "presentation": presentation, "audience": audience, "nonce": nonce,
            "trusted_document": trusted_document, "issuer_url": IDENTITY_ISSUER_URL,
        }),
        capture_output=True, text=True, timeout=8, check=False,
    )
    if result.returncode:
        raise ValueError("钱包身份验证失败")
    value = json.loads(result.stdout)
    return str(value.get("did", ""))


CASE_FIELD_NAMES = {
    "优先级": "priority", "类型": "kind", "状态": "status",
    "前置条件": "preconditions", "步骤": "steps", "预期结果": "expected",
}


def load_test_cases() -> list[dict]:
    """Build mind-map data directly from each project's case source document."""
    result = []
    case_heading = re.compile(r"^###\s+([A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*-\d{3})\s+(.+?)\s*$")
    for path in sorted((REPO / "docs/test-cases").glob("*.md")):
        if path.name == "README.md":
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        title = next((line[2:].strip() for line in lines if line.startswith("# ")), path.stem)
        project = {"id": path.stem, "name": title, "modules": []}
        module = None
        index = 0
        while index < len(lines):
            line = lines[index]
            if line.startswith("## "):
                module = {"id": f"{path.stem}-module-{len(project['modules']) + 1}",
                          "name": line[3:].strip(), "cases": []}
                project["modules"].append(module)
            match = case_heading.match(line)
            if match:
                if module is None:
                    module = {"id": f"{path.stem}-module-uncategorized", "name": "未分类", "cases": []}
                    project["modules"].append(module)
                fields = {name: "" for name in CASE_FIELD_NAMES.values()}
                index += 1
                current = None
                while index < len(lines) and not lines[index].startswith(("### ", "## ")):
                    content = lines[index]
                    if content.startswith("- "):
                        label, separator, value = content[2:].partition(":")
                        current = CASE_FIELD_NAMES.get(label.strip()) if separator else None
                        if current:
                            fields[current] = value.strip()
                    elif current and fields[current] and current not in ("steps",):
                        if content.strip():
                            fields[current] += "\n" + content.strip()
                    elif current == "steps" and content.strip():
                        fields[current] += ("\n" if fields[current] else "") + content.strip()
                    index += 1
                module["cases"].append({
                    "id": match.group(1), "title": match.group(2).strip(), **fields,
                })
                continue
            index += 1
        project["modules"] = [entry for entry in project["modules"] if entry["cases"]]
        project["case_count"] = sum(len(entry["cases"]) for entry in project["modules"])
        if project["case_count"]:
            result.append(project)
    return result


def git_sha(repo: str, remote: bool) -> str:
    command = (
        ["git", "-C", repo, "ls-remote", "origin", "refs/heads/main"]
        if remote else ["git", "-C", repo, "rev-parse", "HEAD"]
    )
    result = subprocess.run(command, capture_output=True, text=True, timeout=35, check=True)
    value = result.stdout.split()[0]
    if len(value) != 40 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError("Invalid Git commit SHA")
    return value


def normalize_batch_item(item: dict) -> dict:
    project = item.get("project")
    if project == FULL:
        item.setdefault("scope", "full")
        item.setdefault("projects", [])
    elif "projects" not in item:
        item["projects"] = [PROJECT_ALIASES.get(project, project)] if project else []
        item["scope"] = "projects"
        if item.get("target_sha") and item["projects"]:
            item.setdefault("target_shas", {})[item["projects"][0]] = item["target_sha"]
    else:
        item["projects"] = [PROJECT_ALIASES.get(name, name) for name in item["projects"]]
        item.setdefault("scope", "projects")
        item.setdefault("target_shas", {})
    item.setdefault("target_shas", {})
    item.setdefault("results", {})
    if item.get("scope") == "projects" and len(item["projects"]) > 1:
        item["project"] = MULTI_PROJECT
    elif item.get("scope") == "projects" and item["projects"]:
        item["project"] = item["projects"][0]
    return item


STATE = load_state()


def _enqueue_batch_locked(projects: list[str] | None, source: str,
                          target_shas: dict[str, str] | None = None,
                          mode: str = "test", target_sha: str | None = None,
                          schedule_date: str | None = None) -> dict:
    is_full = projects is None
    canonical = sorted({PROJECT_ALIASES.get(name, name) for name in (projects or [])})
    if not is_full and (not canonical or any(name not in PROJECTS for name in canonical)):
        raise ValueError("Unknown or empty project batch")
    if is_full:
        mode = "test"
    if mode not in ("test", "deploy"):
        raise ValueError("Unknown test mode")
    if mode == "deploy" and not LOCAL_SOURCE_MODE:
        unsupported = [name for name in canonical
                       if name not in DEPLOYMENT_ADAPTERS or not PROJECTS[name].get("enabled", False)]
        if unsupported:
            raise ValueError("Deployment adapter is not enabled for " + ", ".join(unsupported))
    if ARTIFACT_MODE and mode == "deploy" and not (ALLOW_ARTIFACT_SOURCE_DEPLOY or LOCAL_SOURCE_MODE):
        raise ValueError("Artifact mode does not deploy product source checkouts")
    revisions = {PROJECT_ALIASES.get(name, name): sha for name, sha in (target_shas or {}).items()}
    if target_sha and len(canonical) == 1:
        revisions.setdefault(canonical[0], target_sha)
    if any(not re.fullmatch(r"[0-9a-f]{40}", sha) for sha in revisions.values()):
        raise ValueError("Invalid target commit")
    if target_sha and not re.fullmatch(r"[0-9a-f]{40}", target_sha):
        raise ValueError("Invalid target commit")

    # Keep one pending main-change batch and merge later poll results into it.
    # The active batch is immutable because its projects may already be running.
    active = normalize_batch_item(STATE["active"]) if STATE["active"] else None
    if source == "main" and not is_full:
        active_revisions = (active or {}).get("target_shas", {})
        revisions = {name: sha for name, sha in revisions.items()
                     if active is None or name not in active["projects"]
                     or sha != active_revisions.get(name)}
        if not revisions:
            return active
    elif active and active.get("scope") == ("full" if is_full else "projects") \
            and active.get("projects", []) == canonical and active.get("mode") == mode:
        if source == "daily-schedule" and schedule_date == active.get("schedule_date"):
            return active
        if source not in ("main", "tester-main", "daily-schedule"):
            return active
        if source == "tester-main" and target_sha == active.get("target_sha"):
            return active
    for item in STATE["pending"]:
        normalize_batch_item(item)
        if source == "main" and item.get("source") == "main" and item.get("mode") == mode:
            changed_projects = [
                name for name, sha in revisions.items()
                if item.get("target_shas", {}).get(name) != sha
            ]
            item["target_shas"].update(revisions)
            for name in changed_projects:
                item.get("results", {}).pop(name, None)
            item["projects"] = sorted(item["target_shas"])
            item["project"] = item["projects"][0] if len(item["projects"]) == 1 else MULTI_PROJECT
            item.pop("retry_at", None)
            item.pop("deferred_until", None)
            save_state(STATE)
            return item
        if item.get("scope") == ("full" if is_full else "projects") and item.get("project") == (
            FULL if is_full else (canonical[0] if len(canonical) == 1 else MULTI_PROJECT)
        ) and item.get("projects", []) == canonical and item.get("source") == source and item.get("mode") == mode:
            if source == "daily-schedule" and item.get("schedule_date") != schedule_date:
                continue
            if source == "tester-main" and target_sha:
                item["target_sha"] = target_sha
            if source == "daily-schedule" and schedule_date:
                item["schedule_date"] = schedule_date
                save_state(STATE)
            return item
    item = {
        "id": secrets.token_hex(12),
        "project": FULL if is_full else canonical[0] if len(canonical) == 1 else MULTI_PROJECT,
        "scope": "full" if is_full else "projects",
        "projects": canonical,
        "target_shas": revisions,
        "results": {},
        "mode": mode,
        "source": source,
        "target_sha": target_sha,
        "schedule_date": schedule_date,
        "queued_at": now(),
    }
    STATE["pending"].append(item)
    save_state(STATE)
    return item


def enqueue_batch(projects: list[str] | None, source: str,
                  target_shas: dict[str, str] | None = None,
                  mode: str = "test", target_sha: str | None = None,
                  schedule_date: str | None = None) -> dict:
    with MUTEX:
        return _enqueue_batch_locked(projects, source, target_shas, mode, target_sha, schedule_date)


def enqueue(project: str, source: str, target_sha: str | None = None,
            mode: str = "deploy") -> dict:
    if project == FULL:
        return enqueue_batch(None, source, mode="test", target_sha=target_sha)
    return enqueue_batch([project], source,
                         target_shas={project: target_sha} if target_sha else None,
                         mode=mode, target_sha=target_sha)


def poll_main() -> None:
    if not WATCH_UPSTREAM_MAIN:
        return
    changes = {}
    seen_updates = {}
    for name, config in PROJECTS.items():
        if not config.get("enabled", False):
            continue
        try:
            remote_sha = git_sha(config["repo"], remote=True)
            local_sha = git_sha(config["repo"], remote=False)
            previous = STATE["seen"].get(name)
            if (previous is not None and remote_sha != previous) or (
                previous is None and remote_sha != local_sha
            ):
                changes[name] = remote_sha
            seen_updates[name] = remote_sha
            with MUTEX:
                STATE["poll_errors"].pop(name, None)
                if name not in changes:
                    STATE["seen"][name] = remote_sha
                    save_state(STATE)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            with MUTEX:
                STATE["poll_errors"][name] = f"{type(error).__name__}: upstream main check failed"
                save_state(STATE)
    with MUTEX:
        if changes:
            try:
                enqueue_batch(list(changes), "main", changes, mode="deploy")
            except ValueError:
                for name in changes:
                    STATE["poll_errors"][name] = "upstream change could not be queued for source testing"
                save_state(STATE)
                return
        STATE["seen"].update(seen_updates)
        save_state(STATE)


def poll_tester_main() -> None:
    if not WATCH_UPSTREAM_MAIN or ARTIFACT_MODE:
        return
    if ARTIFACT_MODE:
        return
    try:
        remote_sha = git_sha(str(REPO), remote=True)
        local_sha = git_sha(str(REPO), remote=False)
        with MUTEX:
            previous = STATE["seen"].get(TESTER_MAIN_KEY)
            if (previous is not None and remote_sha != previous) or (
                previous is None and remote_sha != local_sha
            ):
                enqueue(FULL, "tester-main", remote_sha)
            STATE["seen"][TESTER_MAIN_KEY] = remote_sha
            STATE["poll_errors"].pop(TESTER_MAIN_KEY, None)
            save_state(STATE)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        with MUTEX:
            STATE["poll_errors"][TESTER_MAIN_KEY] = f"{type(error).__name__}: Tester main check failed"
            save_state(STATE)


def latest_summary(project: str) -> dict | None:
    path = (PUBLIC / "latest.json" if project == FULL
            else PUBLIC / "projects" / project / "latest.json")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def load_report_history(limit: int = 30) -> list[dict]:
    """Index published summaries in SQLite and return safe API-backed links."""
    sources = [(FULL, PUBLIC / "runs")]
    sources.extend((project, PUBLIC / "projects" / project / "runs") for project in PROJECTS)
    fields = ("status", "started_at", "finished_at", "tests", "passed", "failed", "skipped")
    with open_auth_database() as database:
        for project, runs_dir in sources:
            if not runs_dir.is_dir():
                continue
            try:
                run_dirs = sorted(
                    (path for path in runs_dir.iterdir() if path.is_dir() and not path.is_symlink()),
                    key=lambda path: path.name,
                    reverse=True,
                )
            except OSError:
                continue
            for run_dir in run_dirs:
                if not re.fullmatch(r"[A-Za-z0-9_+.-]{1,80}", run_dir.name):
                    continue
                summary_path = run_dir / "summary.json"
                try:
                    summary = json.loads(summary_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if summary.get("run") != run_dir.name:
                    continue
                values = [summary.get(field) for field in fields]
                database.execute(
                    """INSERT INTO tester_reports(
                           project,run,status,started_at,finished_at,tests,passed,failed,skipped,storage_path,indexed_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(project,run) DO UPDATE SET
                           status=excluded.status,started_at=excluded.started_at,
                           finished_at=excluded.finished_at,tests=excluded.tests,
                           passed=excluded.passed,failed=excluded.failed,skipped=excluded.skipped,
                           storage_path=excluded.storage_path,indexed_at=excluded.indexed_at""",
                    (project, run_dir.name, *values, str(run_dir.relative_to(PUBLIC)), now()),
                )
        database.commit()
        rows = database.execute(
            "SELECT project,run,status,started_at,finished_at,tests,passed,failed,skipped,storage_path "
            "FROM tester_reports ORDER BY finished_at DESC LIMIT ?", (limit,),
        ).fetchall()

    entries = []
    for row in rows:
        record = dict(row)
        project, run = record.pop("project"), record.pop("run")
        storage_path = record.pop("storage_path")
        record["project"] = project
        record["run"] = run
        record["report"] = f"/report/{storage_path}/index.html"
        entries.append(record)
    return entries


def published_report_file(relative_path: str) -> Path | None:
    """Resolve a published report file without permitting traversal or symlinks."""
    if not relative_path or "\\" in relative_path or "\x00" in relative_path:
        return None
    parts = relative_path.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return None
    candidate = PUBLIC.joinpath(*parts)
    if candidate.is_symlink() or not candidate.is_file():
        return None
    try:
        candidate.resolve(strict=True).relative_to(PUBLIC.resolve(strict=True))
    except (OSError, ValueError):
        return None
    return candidate


def cooldown_until(project: str) -> float | None:
    """Router shares one localhost critical-rate-limit bucket across test runs."""
    if project != "router":
        return None
    attempts = (PUBLIC / "last-attempt.txt", PUBLIC / "projects/router/last-attempt.txt")
    modified = []
    for path in attempts:
        try:
            modified.append(path.stat().st_mtime)
        except FileNotFoundError:
            pass
    if not modified:
        return None
    eligible = max(modified) + ROUTER_COOLDOWN_SECONDS
    return eligible if time.time() < eligible else None


def execute(item: dict) -> bool:
    normalize_batch_item(item)
    project = item["project"]
    mode = "test" if item.get("scope") == "full" else item.get("mode", "deploy")
    item.pop("retry_at", None)
    item.pop("deferred_until", None)
    if item.get("scope") == "full":
        projects = [FULL]
    else:
        projects = item["projects"]
    with MUTEX:
        save_state(STATE)
    LOG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    log_path = LOG_DIR / f"{item['id']}.log"
    environment = os.environ.copy()
    environment["TESTER_TRIGGER"] = item["source"]
    try:
        with log_path.open("w", encoding="utf-8") as log:
            if ARTIFACT_MODE:
                environment["TESTER_SOURCE_DIR"] = str(REPO)
                metadata = REPO / "release.json"
                if metadata.exists():
                    environment["TESTER_SOURCE_SHA"] = json.loads(metadata.read_text())["source_commit"]
            reports = {}
            for child_project in projects:
                target_sha = item.get("target_shas", {}).get(child_project)
                if child_project == FULL:
                    target_sha = item.get("target_sha")
                if mode == "deploy" and child_project != FULL and not target_sha:
                    try:
                        target_sha = git_sha(PROJECTS[child_project]["repo"], remote=True)
                        item.setdefault("target_shas", {})[child_project] = target_sha
                    except (OSError, ValueError, subprocess.SubprocessError):
                        return False

                previous = item.get("results", {}).get(child_project)
                if previous and previous.get("completed"):
                    reports[child_project] = previous.get("report")
                    continue

                cooldown_key = "router" if child_project == FULL else child_project
                eligible = cooldown_until(cooldown_key)
                if eligible is not None:
                    item["retry_at"] = eligible
                    item["deferred_until"] = datetime.fromtimestamp(eligible).astimezone().isoformat(timespec="minutes")
                    with MUTEX:
                        save_state(STATE)
                    return False

                child_id = item["id"] if len(projects) == 1 else hashlib.sha256(
                    f"{item['id']}:{child_project}".encode()
                ).hexdigest()[:24]
                child_env = environment.copy()
                child_env["TESTER_REQUEST_ID"] = child_id
                child_env["TESTER_BATCH_ID"] = item["id"]
                child_env["TESTER_BATCH_PROJECT"] = child_project
                child_env["TESTER_TRIGGER"] = item["source"]
                if target_sha:
                    child_env["TESTER_TARGET_SHA"] = target_sha

                if ARTIFACT_MODE and child_project != FULL and mode == "deploy" and not LOCAL_SOURCE_MODE:
                    command = [str(RUNNER), child_project, target_sha]
                elif child_project == FULL and item["source"] == "tester-main":
                    command = [str(TESTER_MAIN_RUNNER), target_sha]
                elif ARTIFACT_MODE:
                    command = [str(FULL_RUNNER)] + ([] if child_project == FULL else [child_project])
                elif mode == "test":
                    command = [str(TESTER_MAIN_RUNNER)] + ([] if child_project == FULL else ["--project", child_project])
                else:
                    command = [str(RUNNER), child_project, target_sha]

                result = subprocess.run(
                    command, cwd=REPO, env=child_env, stdout=log,
                    stderr=subprocess.STDOUT, timeout=9700, check=False,
                )
                if result.returncode == 75:
                    return False

                summary = latest_summary(child_project)
                published = bool(
                    summary and summary.get("request_id") == child_id
                    and (summary.get("tester_sha") == target_sha
                         if item["source"] == "tester-main"
                         else mode == "test" or summary.get("target_sha") == target_sha)
                )
                report = None
                if published:
                    report = (f"/report/runs/{summary['run']}/index.html" if child_project == FULL
                              else f"/report/projects/{child_project}/runs/{summary['run']}/index.html")
                    reports[child_project] = report
                child_result = summary.get("status") if published and result.returncode == 0 else (
                    "failed" if published else "deploy_or_runner_failed"
                )
                item.setdefault("results", {})[child_project] = {
                    "completed": True,
                    "result": child_result,
                    "target_sha": target_sha,
                    "report": report,
                    "finished_at": now(),
                    "counts": {key: summary.get(key) for key in ("tests", "passed", "failed", "skipped")}
                    if published else None,
                }
                with MUTEX:
                    save_state(STATE)

        results = item.get("results", {})
        batch_result = "passed" if all(
            results.get(name, {}).get("result") == "passed" for name in projects
        ) else ("failed" if all(
            results.get(name, {}).get("result") != "deploy_or_runner_failed" for name in projects
        ) else "deploy_or_runner_failed")
        record = {
            **item,
            "finished_at": now(),
            "result": batch_result,
            "reports": reports,
            "report": reports.get(FULL) or (reports.get(projects[0]) if len(projects) == 1 else None),
            "log": str(log_path),
        }
        with MUTEX:
            STATE["recent"] = [record, *STATE["recent"]][:30]
            STATE["active"] = None
            save_state(STATE)
        return True
    except (OSError, subprocess.TimeoutExpired) as error:
        with MUTEX:
            STATE["recent"] = [{
                **item, "finished_at": now(), "result": type(error).__name__,
                "report": None, "log": str(log_path),
            }, *STATE["recent"]][:30]
            STATE["active"] = None
            save_state(STATE)
        return True


def take_ready() -> dict | None:
    """Let ready projects run while a blocked project waits for its retry time."""
    with MUTEX:
        current = time.time()
        for index, pending in enumerate(STATE["pending"]):
            if pending.get("retry_at", 0) <= current:
                item = STATE["pending"].pop(index)
                STATE["active"] = item
                save_state(STATE)
                return item
    return None


def poller() -> None:
    while True:
        poll_main()
        poll_tester_main()
        time.sleep(POLL_SECONDS)


def worker() -> None:
    while True:
        item = take_ready()
        if item:
            if not execute(item):
                with MUTEX:
                    STATE["active"] = None
                    if item.get("retry_at", 0) <= time.time():
                        item["retry_at"] = time.time() + RETRY_SECONDS
                        item["deferred_until"] = datetime.fromtimestamp(
                            item["retry_at"]
                        ).astimezone().isoformat(timespec="minutes")
                    STATE["pending"].append(item)
                    save_state(STATE)
        else:
            time.sleep(2)


def daily_scheduler(schedule: tuple[clock_time, ZoneInfo]) -> None:
    run_at, zone = schedule
    while True:
        with MUTEX:
            scheduled_date = STATE.get("daily_scheduled_date")
        target = next_scheduled_daily_run(datetime.now(timezone.utc), run_at, zone, scheduled_date)
        delay = max(1, (target - datetime.now(zone)).total_seconds())
        time.sleep(delay)
        item = enqueue_daily_run(target)
        if item:
            print(f"Tester daily full run queued: {item['id']} for {target.isoformat()}", flush=True)


def enqueue_daily_run(target: datetime) -> dict | None:
    scheduled_date = target.date().isoformat()
    with MUTEX:
        previous_date = STATE.get("daily_scheduled_date")
        if previous_date is not None and previous_date >= scheduled_date:
            return None
        STATE["daily_scheduled_date"] = scheduled_date
        return enqueue_batch(None, "daily-schedule", schedule_date=scheduled_date)


def overview_payload(include_history: bool = True) -> dict:
    """Use published counts and an allowlist: never expose private runner paths."""
    fields = {"id", "project", "projects", "scope", "source", "started_at",
              "finished_at", "result", "report", "reports", "results"}
    def public_record(item):
        return {key: value for key, value in (item or {}).items() if key in fields}

    full = latest_summary(FULL) or {}
    counts = full.get("summary", full)
    summary = {key: counts.get(key) for key in ("passed", "failed", "skipped")}
    summary["total"] = counts.get("total", counts.get("tests"))
    with MUTEX:
        recent = [public_record(item) for item in STATE["recent"][:30]]
        pending = {item["project"] for item in STATE["pending"]}
        active = (STATE["active"] or {}).get("project")
        projects = {}
        for name in PROJECTS:
            last = next((item for item in recent
                         if item.get("project") == name or name in item.get("projects", [])), None)
            published = latest_summary(name) or {}
            projects[name] = {
                "status": (last or {}).get("result", published.get("status", "pending")),
                "busy": (name == active or name in pending
                         or name in (STATE["active"] or {}).get("projects", [])
                         or any(name in item.get("projects", []) for item in STATE["pending"])),
                "last": last,
            }
    metadata_path = REPO / "release.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.is_file() else {}
    return {
        "service": {"status": "pass", "version": metadata.get("version", "development"),
                    "source_commit": metadata.get("source_commit")},
        "full": {
            "busy": active == FULL or FULL in pending,
            "last": {key: full[key] for key in ("run", "status", "finished_at", "started_at") if key in full},
            "summary": summary,
        },
        "projects": projects,
        "history": load_report_history() if include_history else [],
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        if ARTIFACT_MODE:
            print("Tester control HTTP request", file=sys.stderr)
        else:
            super().log_message(format, *args)

    def reply(self, code: int, payload: bytes, content_type: str, headers: dict[str, str] | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(payload)

    def json_reply(self, code: int, value: dict, headers: dict[str, str] | None = None) -> None:
        self.reply(code, json.dumps(value, ensure_ascii=False).encode(), "application/json; charset=utf-8", headers)

    def origin_matches(self) -> bool:
        return self.headers.get("Origin", "").rstrip("/") == ORIGIN.rstrip("/")

    def current_session(self) -> tuple[str, dict, dict] | None:
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
            token = cookie[SESSION_COOKIE].value if SESSION_COOKIE in cookie else ""
        except Exception:
            return None
        if not token:
            return None
        with AUTH_MUTEX:
            session = AUTH_SESSIONS.get(token)
            if session and session["expires_at"] > time.time():
                user = find_user(session["did"])
                if user and user["status"] == "active":
                    return token, session, user
            AUTH_SESSIONS.pop(token, None)
        return None

    def clear_session_cookie(self) -> None:
        self.send_header("Set-Cookie", f"{SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Lax")

    def analysis_get(self, path: str, query: str) -> None:
        auth = self.headers.get("Authorization", "")
        token = auth.removeprefix("Bearer ") if auth.startswith("Bearer ") else ""
        valid = False
        if analysis.TOKEN.fullmatch(token):
            with open_auth_database() as database:
                valid = database.execute(
                    """SELECT 1 FROM tester_analysis_tokens AS token
                       JOIN tester_users AS owner ON owner.did=token.created_by
                       WHERE token.digest=? AND token.revoked_at IS NULL AND token.expires_at>?
                         AND owner.role='admin' AND owner.status='active'""",
                    (analysis.token_digest(token), now()),
                ).fetchone() is not None
        if not valid:
            self.json_reply(401, {"error": "Valid Tester analysis token required"},
                            {"WWW-Authenticate": "Bearer"})
            return

        route = path.removeprefix("/api/v1/analysis/")
        catalog = {item["id"]: item for item in load_test_cases()}
        if route == "overview":
            value = overview_payload(include_history=False)
            full_run = value["full"]["last"].get("run")
            full_evidence = None
            if isinstance(full_run, str) and analysis.RUN_ID.fullmatch(full_run):
                full_evidence = analysis.load_run_evidence(PRIVATE.parent / full_run, PUBLIC / "runs" / full_run)
            value.pop("history")
            with MUTEX:
                value["queue"] = {"active": (STATE["active"] or {}).get("project"),
                                  "queued": len(STATE["pending"])}
            for name in sorted(set(catalog) | set(PROJECTS)):
                project = value["projects"].setdefault(name, {"status": "untracked", "busy": False})
                project["documented_cases"] = catalog.get(name, {}).get("case_count", 0)
                project["watcher_enabled"] = bool(PROJECTS.get(name, {}).get("enabled"))
                last = latest_summary(name)
                project["latest_published"] = {
                    key: last.get(key) for key in ("run", "status", "finished_at", "tests", "passed", "failed", "skipped")
                } if last else None
                if full_evidence is not None:
                    history = analysis.RunHistory(PRIVATE.parent, PUBLIC, name, REPO)
                    full_result = history.detail(FULL, full_run, full_evidence)
                    project["latest_full_batch"] = (
                        {key: full_result[key] for key in ("run", "stage", "evidence", "counts", "report")}
                        if full_result else None
                    )
            self.json_reply(200, value)
            return

        parts = route.split("/")
        if len(parts) < 2 or parts[0] != "projects" or parts[1] not in set(catalog) | set(PROJECTS):
            self.json_reply(404, {"error": "Project or endpoint not found"})
            return
        name = parts[1]
        if len(parts) == 2:
            self.json_reply(200, {
                "project": name, "catalog": catalog.get(name),
                "configuration": analysis.project_configuration(REPO, name, PROJECTS.get(name, {}), LOCAL_SOURCE_MODE),
            })
            return

        history = analysis.RunHistory(PRIVATE.parent, PUBLIC, name, REPO)
        try:
            params = parse_qs(query, keep_blank_values=True, max_num_fields=12)
            if len(parts) == 5 and parts[2] == "runs":
                scope, encoded_run = parts[3:]
                run = unquote(encoded_run)
                if scope not in (name, FULL) or not analysis.RUN_ID.fullmatch(run):
                    raise ValueError()
                datetime.strptime(run, analysis.STAMP_FORMAT)
                record = history.detail(scope, run)
                if record is None:
                    self.json_reply(404, {"error": "Run not found for this project"})
                    return
                self.json_reply(200, record)
                return

            if len(parts) == 3 and parts[2] in ("runs", "stability"):
                requested = parts[2]
                limit = int(params.get("limit", ["20"])[0])
                if not 1 <= limit <= 50:
                    raise ValueError()
                mode = params.get("mode", [None])[0]
                if mode not in (None, "deployed", "local-source", "unknown"):
                    raise ValueError()
                before = None
                if "before" in params:
                    run, scope = params["before"][0].split("/")
                    if scope not in (FULL, name) or not analysis.RUN_ID.fullmatch(run):
                        raise ValueError()
                    before = datetime.strptime(run, analysis.STAMP_FORMAT).timestamp(), run, scope
                runs = history.list(limit + 1, before, mode)
                page = runs[:limit]
                cursor = f"{page[-1]['run']}/{page[-1]['scope']}" if len(runs) > limit else None
                batches = [{key: item for key, item in run.items() if key not in ("cases", "config")}
                           for run in page]
                value = {"project": name, "runs": batches, "next_cursor": cursor}
                if requested == "stability":
                    value["groups"] = analysis.stability(page)
                self.json_reply(200, value)
                return
        except ValueError:
            self.json_reply(400, {"error": "Invalid run ID or filter"})
            return
        self.json_reply(404, {"error": "Endpoint not found"})

    def analysis_token_write(self, path: str, user: dict) -> None:
        if user["role"] != "admin":
            self.json_reply(403, {"error": "管理员权限不足"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 1024:
                raise ValueError()
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError()
            if path.endswith("/revoke"):
                token_id = body.get("id")
                if not isinstance(token_id, str) or not re.fullmatch(r"[0-9a-f]{24}", token_id):
                    raise ValueError()
                with open_auth_database() as database:
                    changed = database.execute("UPDATE tester_analysis_tokens SET revoked_at=? WHERE id=?",
                                               (now(), token_id)).rowcount
                self.json_reply(200 if changed else 404, {"ok": bool(changed)})
                return
            label = body.get("label")
            days = body.get("days", 30)
            if (not isinstance(label, str) or not 1 <= len(label.strip()) <= 80
                    or type(days) is not int or not 1 <= days <= 90):
                raise ValueError()
        except ValueError:
            self.json_reply(400, {"error": "Token 名称、有效期或 ID 无效"})
            return

        token = "yt_" + secrets.token_urlsafe(32)
        token_id = secrets.token_hex(12)
        expires_at = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(timespec="seconds")
        with open_auth_database() as database:
            database.execute(
                """INSERT INTO tester_analysis_tokens(id,digest,label,created_by,created_at,expires_at)
                   VALUES(?,?,?,?,?,?)""",
                (token_id, analysis.token_digest(token), label.strip(), user["did"], now(), expires_at),
            )
        self.json_reply(201, {"id": token_id, "expires_at": expires_at, "token": token})

    def do_GET(self) -> None:
        url = urlsplit(self.path)
        path = url.path
        if path.startswith("/api/v1/analysis/"):
            self.analysis_get(path, url.query)
            return
        if path == "/api/auth/analysis-tokens":
            current = self.current_session()
            if not current or current[2]["role"] != "admin":
                self.json_reply(403 if current else 401, {"error": "管理员权限不足"})
                return
            with open_auth_database() as database:
                rows = database.execute(
                    """SELECT id,label,created_at,expires_at,revoked_at FROM tester_analysis_tokens
                       ORDER BY created_at DESC, id"""
                ).fetchall()
            self.json_reply(200, {"tokens": [dict(row) for row in rows]})
            return
        if path == "/healthz":
            metadata = REPO / "release.json"
            value = json.loads(metadata.read_text()) if metadata.exists() else {}
            self.json_reply(200, {"status": "pass", "unit": "tester",
                                  "version": value.get("version", "development"),
                                  "source_commit": value.get("source_commit"),
                                  "artifact_mode": ARTIFACT_MODE, "pid": os.getpid(),
                                  "service_pid": int(os.environ.get("TESTER_SERVICE_PID", os.getpid()))})
            return
        if path == "/api/architecture":
            topology = json.loads((REPO / "docs/project-dependency-map.json").read_text(encoding="utf-8"))
            self.json_reply(200, topology)
            return
        if path == "/api/reports":
            if not self.current_session():
                self.json_reply(401, {"error": "需要钱包身份登录"})
                return
            self.json_reply(200, {"reports": load_report_history(limit=200)})
            return
        if path.startswith("/api/reports/files/"):
            if not self.current_session():
                self.json_reply(401, {"error": "需要钱包身份登录"})
                return
            relative_path = path.removeprefix("/api/reports/files/") or "index.html"
            target = published_report_file(relative_path)
            if target is None:
                self.json_reply(404, {"error": "报告文件不存在"})
                return
            content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(target.stat().st_size))
            self.send_header("Cache-Control", "private, no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            with target.open("rb") as report_file:
                while chunk := report_file.read(64 * 1024):
                    self.wfile.write(chunk)
            return
        if path == "/api/overview":
            if not self.current_session():
                self.json_reply(401, {"error": "需要钱包身份登录"})
                return
            self.json_reply(200, overview_payload())
            return
        if path == "/api/auth/status":
            current = self.current_session()
            if not current:
                self.json_reply(401, {"authenticated": False})
                return
            _, session, user = current
            self.json_reply(200, {"authenticated": True, "did": session["did"], "csrf": session["csrf"], "role": user["role"]})
            return
        if path == "/api/auth/users":
            current = self.current_session()
            if not current or current[2]["role"] != "admin":
                self.json_reply(403 if current else 401, {"error": "管理员权限不足"})
                return
            self.json_reply(200, list_users())
            return
        if path == "/api/cases":
            if not self.current_session():
                self.json_reply(401, {"error": "需要钱包身份登录"})
                return
            self.json_reply(200, load_test_cases())
            return
        if path != "/api/status":
            self.json_reply(404, {"error": "Not found"})
            return
        if not self.current_session():
            self.json_reply(401, {"error": "需要钱包身份登录"})
            return
        with MUTEX:
            def state_for(project: str, config: dict | None = None) -> dict:
                busy = None
                active_item = normalize_batch_item(STATE["active"]) if STATE["active"] else None
                project_active = active_item and (
                    active_item.get("project") == project or project in active_item.get("projects", [])
                )
                pending_items = [item for item in STATE["pending"] if (
                    item.get("project") == project or project in item.get("projects", [])
                )]
                if project_active:
                    busy = ("正在部署或测试" if STATE["active"].get("mode", "deploy") == "deploy"
                            and project != FULL else "正在测试")
                elif pending_items:
                    pending = pending_items[0]
                    busy = ("已排队，预计重试 " + pending["deferred_until"]
                            if pending.get("deferred_until") else "已排队")
                last = next((item for item in STATE["recent"]
                             if item.get("project") == project or project in item.get("projects", [])), None)
                if last:
                    last = {key: value for key, value in last.items() if key != "log"}
                result = {"busy": busy, "last": last}
                if config is not None:
                    result.update({
                        "enabled": bool(config.get("enabled", False)),
                        "reason": config.get("reason") if not config.get("enabled", False) else None,
                        "poll_error": STATE["poll_errors"].get(project),
                    })
                return result
            values = {}
            for name, config in PROJECTS.items():
                values[name] = state_for(name, config)
            full = state_for(FULL)
            def public_task(item: dict | None) -> dict | None:
                if not item:
                    return None
                normalize_batch_item(item)
                return {
                    "batch_id": item.get("id"),
                    "project": item.get("project"),
                    "projects": item.get("projects", []),
                    "scope": item.get("scope"),
                    "source": item.get("source"),
                    "mode": item.get("mode", "deploy"),
                    "target_sha": item.get("target_sha"),
                    "target_shas": item.get("target_shas", {}),
                    "schedule_date": item.get("schedule_date"),
                    "queued_at": item.get("queued_at"),
                    "deferred_until": item.get("deferred_until"),
                }

            queue = {
                "parallelism": 1,
                "active": public_task(STATE["active"]),
                "pending": [public_task(item) for item in STATE["pending"]],
                "recent": [
                    {
                        "batch_id": item.get("id"),
                        **{key: value for key, value in item.items()
                           if key in ("id", "project", "projects", "scope", "source", "mode",
                                      "target_sha", "target_shas", "schedule_date", "queued_at",
                                      "finished_at", "result", "reports", "results")},
                    }
                    for item in STATE["recent"][:12]
                ],
            }
        self.json_reply(200, {"full": full, "projects": values, "queue": queue})

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if path not in (
            "/api/auth/identity/login/session",
            "/api/auth/identity/login/verify",
            "/api/auth/logout",
            "/api/auth/analysis-tokens",
            "/api/auth/analysis-tokens/revoke",
            "/api/auth/users/approve",
            "/api/auth/users/revoke",
            "/api/run",
        ):
            self.json_reply(404, {"error": "Not found"})
            return
        if not self.origin_matches():
            self.json_reply(403, {"error": "请刷新页面后重试"})
            return
        if path == "/api/auth/identity/login/session":
            with AUTH_MUTEX:
                current = time.time()
                for key in [key for key, value in LOGIN_SESSIONS.items() if value["expires_at"] <= current]:
                    LOGIN_SESSIONS.pop(key, None)
                session_id = secrets.token_urlsafe(32)
                nonce = secrets.token_urlsafe(32)
                LOGIN_SESSIONS[session_id] = {
                    "nonce": nonce, "audience": ORIGIN.rstrip("/"),
                    "expires_at": current + LOGIN_TTL_SECONDS,
                }
            self.json_reply(200, {"code": 0, "message": "ok", "data": {
                "session_id": session_id, "nonce": nonce, "audience": ORIGIN.rstrip("/"),
                "issuerEndpoint": IDENTITY_ISSUER_URL,
                "scopes": ["identity.basic", "identity.wallet", "identity.username", "identity.email"],
                "expires_at": datetime.fromtimestamp(current + LOGIN_TTL_SECONDS, timezone.utc).isoformat(),
            }})
            return
        if path == "/api/auth/identity/login/verify":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 1 or length > 65536:
                    raise ValueError("登录请求大小不正确")
                body = json.loads(self.rfile.read(length))
                session_id = str(body.get("session_id", "")) if isinstance(body, dict) else ""
                presentation = body.get("presentation") if isinstance(body, dict) else None
                if not session_id or not isinstance(presentation, dict):
                    raise ValueError("登录参数无效")
                with AUTH_MUTEX:
                    login = LOGIN_SESSIONS.pop(session_id, None)
                if not login or login["expires_at"] <= time.time():
                    raise ValueError("登录请求无效或已过期")
                claimed_did = str(presentation.get("holder", ""))
                user = find_user(claimed_did)
                trusted_document = json.loads(user["identity_document"]) if user and user.get("identity_document") else None
                did = verify_wallet_presentation(presentation, login["audience"], login["nonce"], trusted_document)
                if user is None:
                    identity_document = json.dumps(presentation["identityDocument"], ensure_ascii=False, separators=(",", ":"))
                    with open_auth_database() as database:
                        database.execute(
                            "INSERT OR IGNORE INTO tester_users(did,identity_document,display_name,role,status,requested_at) VALUES(?,?,?,'member','pending',?)",
                            (did, identity_document, did[-10:], now()),
                        )
                    self.json_reply(403, {"code": 1, "message": "账号等待管理员审批", "data": {"did": did}})
                    return
                if user["status"] == "pending":
                    self.json_reply(403, {"code": 1, "message": "账号等待管理员审批", "data": {"did": did}})
                    return
                if user["status"] != "active":
                    self.json_reply(403, {"code": 1, "message": "此 Tester 账号已停用"})
                    return
                if trusted_document is None:
                    identity_document = json.dumps(presentation["identityDocument"], ensure_ascii=False, separators=(",", ":"))
                    with open_auth_database() as database:
                        database.execute("UPDATE tester_users SET identity_document=? WHERE did=? AND role='admin' AND identity_document IS NULL", (identity_document, did))
                with open_auth_database() as database:
                    database.execute("UPDATE tester_users SET last_login_at=? WHERE did=? AND status='active'", (now(), did))
                token = secrets.token_urlsafe(48)
                session = {"did": did, "csrf": secrets.token_urlsafe(32), "expires_at": time.time() + SESSION_TTL_SECONDS}
                with AUTH_MUTEX:
                    for key in [key for key, value in AUTH_SESSIONS.items() if value["expires_at"] <= time.time()]:
                        AUTH_SESSIONS.pop(key, None)
                    AUTH_SESSIONS[token] = session
                self.json_reply(200, {"code": 0, "message": "ok", "data": {"token": token, "did": did}}, {
                    "Set-Cookie": f"{SESSION_COOKIE}={token}; Path=/; Max-Age={SESSION_TTL_SECONDS}; HttpOnly; Secure; SameSite=Lax"
                })
            except (ValueError, KeyError, json.JSONDecodeError, subprocess.SubprocessError) as error:
                self.json_reply(401, {"code": 1, "message": str(error)})
            return
        if path == "/api/auth/logout":
            current = self.current_session()
            if not current or self.headers.get("X-Tester-CSRF") != current[1]["csrf"]:
                self.json_reply(403, {"error": "会话无效，请刷新页面后重试"})
                return
            with AUTH_MUTEX:
                AUTH_SESSIONS.pop(current[0], None)
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.clear_session_cookie()
            payload = json.dumps({"ok": True}).encode()
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        current = self.current_session()
        if not current or self.headers.get("X-Tester-CSRF") != current[1]["csrf"]:
            self.json_reply(401 if not current else 403, {"error": "请先登录或刷新页面后重试"})
            return
        if path in ("/api/auth/analysis-tokens", "/api/auth/analysis-tokens/revoke"):
            self.analysis_token_write(path, current[2])
            return
        if path in ("/api/auth/users/approve", "/api/auth/users/revoke"):
            if current[2]["role"] != "admin":
                self.json_reply(403, {"error": "管理员权限不足"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 1 or length > 1024:
                    raise ValueError("请求大小不正确")
                body = json.loads(self.rfile.read(length))
                did = str(body.get("did", "")).strip() if isinstance(body, dict) else ""
                if not did or did == current[2]["did"]:
                    raise ValueError("不能修改当前管理员账号")
                status = "active" if path.endswith("approve") else "disabled"
                with open_auth_database() as database:
                    result = database.execute(
                        "UPDATE tester_users SET status=?,approved_at=CASE WHEN ?='active' THEN ? ELSE approved_at END,approved_by=? WHERE did=? AND role='member'",
                        (status, status, now(), current[2]["did"], did),
                    )
                if result.rowcount != 1:
                    raise ValueError("成员不存在或不可修改")
                self.json_reply(200, {"ok": True, "did": did, "status": status})
            except (ValueError, json.JSONDecodeError) as error:
                self.json_reply(400, {"error": str(error)})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 1 or length > 1024:
                raise ValueError("请求大小不正确")
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError("请求内容不正确")
            if body.get("mode", "test") != "test":
                raise ValueError("手动测试仅支持当前环境")
            scope = body.get("scope")
            if scope == "full":
                if "project" in body or "projects" in body:
                    raise ValueError("全量测试不能同时指定项目")
                item = enqueue_batch(None, "manual")
            elif "projects" in body:
                selected_projects = body["projects"]
                if scope != "projects" or not isinstance(selected_projects, list) \
                        or any(not isinstance(name, str) for name in selected_projects):
                    raise ValueError("测试项目列表无效")
                item = enqueue_batch(selected_projects, "manual", mode="test")
            else:
                if scope not in (None, "projects"):
                    raise ValueError("测试范围无效")
                project = body.get("project")
                if not isinstance(project, str) or project not in PROJECTS:
                    raise ValueError("未知项目")
                item = enqueue_batch([project], "manual", mode="test")
            self.json_reply(202, {
                "id": item["id"], "batch_id": item["id"],
                "status": "queued", "scope": item.get("scope"),
                "projects": item.get("projects", []),
            })
        except (ValueError, KeyError, json.JSONDecodeError) as error:
            self.json_reply(400, {"error": str(error)})


def main() -> None:
    PRIVATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    migrate_auth_database()
    bootstrap_admin()
    if not ADMIN_DID:
        print("Tester wallet control is locked: TESTER_ADMIN_DID is not configured", flush=True)
    schedule = daily_schedule()
    if schedule is None:
        print("Tester daily full run is disabled", flush=True)
    else:
        run_at, zone = schedule
        print(f"Tester daily full run enabled: {run_at.strftime('%H:%M')} {zone.key}", flush=True)
    threading.Thread(target=poller, daemon=True).start()
    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    if schedule is not None:
        threading.Thread(target=daily_scheduler, args=(schedule,), daemon=True).start()
    host = os.environ.get("TESTER_CONTROL_HOST", "127.0.0.1")
    port = int(os.environ.get("TESTER_CONTROL_PORT", "18082"))
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()

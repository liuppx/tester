#!/usr/bin/env python3
"""Queue per-project Tester runs and watch upstream main branches on the test host."""

from __future__ import annotations

from datetime import date, datetime, time as clock_time, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
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
from urllib.parse import urlsplit


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
TESTER_MAIN_KEY = "__tester_main__"
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


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_config() -> dict[str, dict]:
    raw = json.loads(CONFIG.read_text(encoding="utf-8"))
    projects = raw.get("projects")
    if not isinstance(projects, dict) or not projects:
        raise ValueError("No Tester projects configured")
    for name, value in projects.items():
        if not name.replace("-", "").isalnum() or not isinstance(value, dict):
            raise ValueError("Invalid project configuration")
        repo = Path(value.get("repo", ""))
        if not repo.is_absolute() or (
            value.get("enabled", False) and not (repo / ".git").exists()
        ):
            raise ValueError(f"Project repository is missing: {name}")
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
        save_state(state)
    return state


STATE = load_state()


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


def enqueue(project: str, source: str, target_sha: str | None = None,
            mode: str = "deploy") -> dict:
    if project == FULL:
        mode = "test"
    elif project not in PROJECTS:
        raise ValueError(f"Unknown project: {project}")
    if ARTIFACT_MODE and mode == "deploy" and not ALLOW_ARTIFACT_SOURCE_DEPLOY:
        raise ValueError("Artifact mode does not deploy product source checkouts")
    if mode not in ("test", "deploy"):
        raise ValueError("Unknown test mode")
    if project != FULL and mode == "deploy" and not PROJECTS[project].get("enabled", False):
        raise ValueError(f"Deployment adapter is not enabled for {project}")
    # An active run has already selected its commit. A newer main commit must
    # remain in the queue rather than rewriting the running request's SHA.
    active = STATE["active"]
    if active and active["project"] == project and active.get("mode", "deploy") == mode:
        if source not in ("main", "tester-main") or not target_sha or target_sha == active.get("target_sha"):
            return active
    for item in STATE["pending"]:
        if item["project"] == project and item.get("mode", "deploy") == mode:
            if source in ("main", "tester-main") and target_sha:
                item["target_sha"] = target_sha
                item["source"] = source
                item.pop("retry_at", None)
                item.pop("deferred_until", None)
                save_state(STATE)
            return item
    item = {
        "id": secrets.token_hex(12),
        "project": project,
        "mode": mode,
        "source": source,
        "target_sha": target_sha,
        "queued_at": now(),
    }
    STATE["pending"].append(item)
    save_state(STATE)
    return item


def poll_main() -> None:
    if not WATCH_UPSTREAM_MAIN:
        return
    for name, config in PROJECTS.items():
        if not config.get("enabled", False):
            continue
        try:
            remote_sha = git_sha(config["repo"], remote=True)
            local_sha = git_sha(config["repo"], remote=False)
            with MUTEX:
                previous = STATE["seen"].get(name)
                if (previous is not None and remote_sha != previous) or (
                    previous is None and remote_sha != local_sha
                ):
                    enqueue(name, "main", remote_sha)
                STATE["seen"][name] = remote_sha
                STATE["poll_errors"].pop(name, None)
                save_state(STATE)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            with MUTEX:
                STATE["poll_errors"][name] = f"{type(error).__name__}: upstream main check failed"
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
    """Read only published summary fields and construct safe public report links."""
    entries = []
    sources = [(FULL, PUBLIC / "runs")]
    sources.extend((project, PUBLIC / "projects" / project / "runs") for project in PROJECTS)
    fields = ("run", "started_at", "finished_at", "status", "tests", "passed", "failed", "skipped")
    for project, runs_dir in sources:
        if not runs_dir.is_dir():
            continue
        try:
            run_dirs = sorted((path for path in runs_dir.iterdir() if path.is_dir() and not path.is_symlink()),
                              key=lambda path: path.name, reverse=True)[:limit]
        except OSError:
            continue
        for run_dir in run_dirs:
            summary_path = run_dir / "summary.json"
            try:
                summary = json.loads(summary_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            run = summary.get("run")
            if (not isinstance(run, str) or run != run_dir.name
                    or not re.fullmatch(r"[A-Za-z0-9_+.-]{1,80}", run)):
                continue
            record = {key: summary[key] for key in fields if key in summary}
            record["project"] = project
            record["report"] = (f"/report/runs/{run}/index.html" if project == FULL
                                else f"/report/projects/{project}/runs/{run}/index.html")
            entries.append(record)
    entries.sort(key=lambda item: item.get("finished_at", ""), reverse=True)
    return entries[:limit]


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
    project = item["project"]
    mode = "test" if project == FULL else item.get("mode", "deploy")
    item.pop("retry_at", None)
    item.pop("deferred_until", None)
    eligible = cooldown_until("router" if project == FULL else project)
    if eligible is not None:
        item["retry_at"] = eligible
        item["deferred_until"] = datetime.fromtimestamp(eligible).astimezone().isoformat(timespec="minutes")
        with MUTEX:
            save_state(STATE)
        return False
    item.pop("deferred_until", None)
    target_sha = None
    if mode == "deploy":
        try:
            target_sha = git_sha(PROJECTS[project]["repo"], remote=True)
        except (OSError, ValueError, subprocess.SubprocessError):
            return False
        item["target_sha"] = target_sha
    with MUTEX:
        save_state(STATE)
    LOG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    log_path = LOG_DIR / f"{item['id']}.log"
    environment = os.environ.copy()
    environment["TESTER_REQUEST_ID"] = item["id"]
    environment["TESTER_TRIGGER"] = item["source"]
    try:
        with log_path.open("w", encoding="utf-8") as log:
            if ARTIFACT_MODE:
                environment["TESTER_SOURCE_DIR"] = str(REPO)
                metadata = REPO / "release.json"
                if metadata.exists():
                    environment["TESTER_SOURCE_SHA"] = json.loads(metadata.read_text())["source_commit"]
                if project != FULL and mode == "deploy":
                    # The Tester artifact is released by Deployer, while the
                    # packaged control service still watches approved product
                    # mains and updates their isolated test deployments.
                    command = [str(RUNNER), project, target_sha]
                else:
                    command = [str(FULL_RUNNER)] + ([] if project == FULL else [project])
            elif project == FULL and item["source"] == "tester-main":
                command = [str(TESTER_MAIN_RUNNER), item["target_sha"]]
            elif mode == "test":
                command = [str(TESTER_MAIN_RUNNER)] + ([] if project == FULL else ["--project", project])
            else:
                command = [str(RUNNER), project, target_sha]
            result = subprocess.run(
                command,
                cwd=REPO,
                env=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=9700,
                check=False,
            )
        if result.returncode == 75:
            return False
        summary = latest_summary(project)
        published = bool(
            summary
            and summary.get("request_id") == item["id"]
            and (summary.get("tester_sha") == item["target_sha"]
                 if item["source"] == "tester-main"
                 else mode == "test" or summary.get("target_sha") == target_sha)
        )
        record = {
            **item,
            "finished_at": now(),
            "result": summary["status"] if published else "deploy_or_runner_failed",
            "report": (f"/report/runs/{summary['run']}/index.html" if project == FULL
                       else f"/report/projects/{project}/runs/{summary['run']}/index.html") if published else None,
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
        target = next_daily_run(datetime.now(timezone.utc), run_at, zone)
        delay = max(1, (target - datetime.now(zone)).total_seconds())
        time.sleep(delay)
        with MUTEX:
            item = enqueue(FULL, "daily-schedule")
        print(f"Tester daily full run queued: {item['id']} for {target.isoformat()}", flush=True)


def overview_payload() -> dict:
    """Use published counts and an allowlist: never expose private runner paths."""
    fields = {"id", "project", "source", "started_at", "finished_at", "result", "report"}
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
            last = next((item for item in recent if item.get("project") == name), None)
            published = latest_summary(name) or {}
            projects[name] = {
                "status": (last or {}).get("result", published.get("status", "pending")),
                "busy": name == active or name in pending,
                "last": last,
            }
    metadata_path = REPO / "release.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.is_file() else {}
    return {
        "service": {"status": "pass", "version": metadata.get("version", "development")},
        "full": {
            "busy": active == FULL or FULL in pending,
            "last": {key: full[key] for key in ("run", "status", "finished_at", "started_at") if key in full},
            "summary": summary,
        },
        "projects": projects,
        "history": load_report_history(),
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

    def current_session(self) -> tuple[str, dict] | None:
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

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
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
                if STATE["active"] and STATE["active"]["project"] == project:
                    busy = ("正在部署或测试" if STATE["active"].get("mode", "deploy") == "deploy"
                            and project != FULL else "正在测试")
                elif any(item["project"] == project for item in STATE["pending"]):
                    pending = next(item for item in STATE["pending"] if item["project"] == project)
                    busy = ("已排队，预计重试 " + pending["deferred_until"]
                            if pending.get("deferred_until") else "已排队")
                last = next((item for item in STATE["recent"] if item["project"] == project), None)
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
        self.json_reply(200, {"full": full, "projects": values})

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if path not in (
            "/api/auth/identity/login/session",
            "/api/auth/identity/login/verify",
            "/api/auth/logout",
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
            if body.get("scope") == "full" and "project" not in body:
                project = FULL
            else:
                project = body["project"]
            if not isinstance(project, str) or (project != FULL and project not in PROJECTS):
                raise ValueError("未知项目")
            if project == FULL and body.get("scope") != "full":
                raise ValueError("请选择测试范围")
            with MUTEX:
                item = enqueue(project, "manual", mode="test")
            self.json_reply(202, {"id": item["id"], "status": "queued"})
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

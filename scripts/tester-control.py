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


PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tester 手动测试</title><style>
:root{color-scheme:light;--ink:#152238;--muted:#66758a;--line:#dce4ee;--blue:#1768bf;--bg:#f3f6fa}
*{box-sizing:border-box}body{font:16px system-ui,-apple-system,sans-serif;background:var(--bg);color:var(--ink);margin:0}
main{max-width:900px;margin:44px auto;padding:0 20px}h1{font-size:28px;margin:8px 0}.eyebrow{font-size:12px;letter-spacing:.12em;color:var(--blue);font-weight:700;text-transform:uppercase}
article{background:#fff;border:1px solid var(--line);border-radius:14px;padding:22px 24px;margin:16px 0;box-shadow:0 8px 24px #1835570a}
.row{display:flex;align-items:center;gap:12px;flex-wrap:wrap}button{background:var(--blue);color:#fff;border:0;border-radius:8px;padding:10px 16px;font-weight:650;cursor:pointer}
button:disabled{background:#a6b1bb;cursor:default}a{color:var(--blue)}select{padding:9px;border:1px solid #b7c4d2;border-radius:8px;min-width:220px;background:white}
.muted{color:var(--muted)}#message{min-height:24px}.top{display:flex;justify-content:space-between;align-items:center;gap:16px}.identity{font-size:13px;overflow-wrap:anywhere}.danger{color:#9e3e2e}
</style></head><body><main><div class="top"><div><div class="eyebrow">Yeying community · quality gate</div><h1>Tester 手动测试</h1></div><p><a href="cases">查看测试用例脑图</a>　<a href="architecture">查看项目依赖图</a>　<a href="/report/">返回测试报告 →</a></p></div>
<p class="muted">授权后可以查看测试状态，并运行当前已部署环境的端到端用例。</p>
<article id="login-card" hidden><h2>钱包身份登录</h2><p class="muted">使用已加入 Tester 授权名单的钱包身份继续。</p><button id="login">连接钱包身份</button><p id="login-message" class="danger" role="status"></p></article>
<section id="console" hidden><article><div class="top"><div><strong>已授权身份</strong><div id="identity" class="identity muted"></div></div><button id="logout">退出登录</button></div></article>
<article id="admin-tools" hidden><button id="users-toggle">成员授权</button><section id="user-panel" hidden><p class="muted">待授权用户完成一次钱包身份登录申请后会列在这里。</p><div id="user-list"></div></section></article>
<article><div class="row"><label for="scope">测试范围</label><select id="scope"><option value="">请选择</option></select><button id="run" disabled>开始测试</button></div>
<p id="selected-status" class="muted"></p><p id="message" role="status"></p><a id="latest-report" hidden>查看上次报告 →</a></article></section>
<script src="web3-bs.js"></script><script>
const scope=document.querySelector("#scope"),run=document.querySelector("#run"),status=document.querySelector("#selected-status"),report=document.querySelector("#latest-report");
let latest=null,csrf="";
function updateSelection(){if(!latest)return;const selected=scope.value,info=selected==="__full__"?latest.full:latest.projects[selected];run.disabled=!info||!!info.busy;status.textContent=info?(info.busy||(info.last?"上次："+info.last.result+" · "+info.last.finished_at:"可运行")):"";report.hidden=!info?.last?.report;if(!report.hidden)report.href=info.last.report}
scope.onchange=updateSelection;
async function loadUsers(){const response=await fetch("api/auth/users",{cache:"no-store"});if(!response.ok)throw new Error("用户列表获取失败");const users=await response.json(),list=document.querySelector("#user-list");list.replaceChildren();for(const user of users){const row=document.createElement("article"),title=document.createElement("strong"),did=document.createElement("p"),state=document.createElement("p");title.textContent=user.display_name||user.did.slice(-10);did.className="identity muted";did.textContent=user.did;state.className="muted";state.textContent=user.role==="admin"?"管理员":user.status==="pending"?"等待授权":user.status==="active"?"已授权":"已停用";row.append(title,did,state);if(user.role!=="admin"){const action=document.createElement("button");action.textContent=user.status==="pending"?"批准访问":user.status==="active"?"撤销访问":"重新授权";action.onclick=async()=>{await fetch("api/auth/users/"+(user.status==="pending"||user.status==="disabled"?"approve":"revoke"),{method:"POST",headers:{"Content-Type":"application/json","X-Tester-CSRF":csrf},body:JSON.stringify({did:user.did})});loadUsers()};row.append(action)}list.append(row)}}
async function refresh(){try{const auth=await fetch("api/auth/status",{cache:"no-store"}),identity=await auth.json();if(!auth.ok||!identity.authenticated){document.querySelector("#login-card").hidden=false;document.querySelector("#console").hidden=true;return}csrf=identity.csrf;document.querySelector("#login-card").hidden=true;document.querySelector("#console").hidden=false;document.querySelector("#identity").textContent=identity.did;document.querySelector("#admin-tools").hidden=identity.role!=="admin";const response=await fetch("api/status",{cache:"no-store"});if(!response.ok)throw new Error();latest=await response.json();const previous=scope.value;scope.replaceChildren(new Option("请选择",""),new Option("全量测试","__full__"));for(const name of Object.keys(latest.projects))scope.add(new Option(name,name));scope.value=previous;updateSelection()}catch{latest=null;run.disabled=true;report.hidden=true;status.textContent="状态获取失败，请刷新页面重试"}}
document.querySelector("#users-toggle").onclick=async()=>{const panel=document.querySelector("#user-panel");panel.hidden=!panel.hidden;if(!panel.hidden)await loadUsers()};
document.querySelector("#login").onclick=async()=>{const message=document.querySelector("#login-message");message.textContent="请在钱包中确认身份授权…";try{if(!window.YeYingWeb3?.loginWithWalletIdentity)throw new Error("钱包登录组件暂不可用，请刷新后重试");const result=await YeYingWeb3.loginWithWalletIdentity({baseUrl:"api/auth",provider:window.ethereum,scopes:["identity.basic","identity.wallet","identity.username","identity.email"],storeToken:false});message.textContent="";document.querySelector("#identity").textContent=result.did;await refresh()}catch(error){const messageText=String(error?.payload?.message||error?.message||"");message.textContent=messageText.includes("等待管理员审批")?"授权申请已提交，管理员批准后即可登录。":messageText.includes("授权")?"钱包身份暂不可用，请确认已完成 Node 邮箱验证和钱包绑定后重试。":"登录未完成，请确认钱包插件可用并重试。"}};
document.querySelector("#logout").onclick=async()=>{await fetch("api/auth/logout",{method:"POST",headers:{"X-Tester-CSRF":csrf}});csrf="";await refresh()};
run.onclick=async()=>{const selected=scope.value;if(!selected||run.disabled)return;run.disabled=true;try{const result=await fetch("api/run",{method:"POST",headers:{"Content-Type":"application/json","X-Tester-CSRF":csrf},body:JSON.stringify(selected==="__full__"?{scope:"full"}:{project:selected})});const body=await result.json();document.querySelector("#message").textContent=result.ok?(selected==="__full__"?"全量测试":selected)+" 已排队，页面会自动更新。":(body.error||"提交失败")}catch{document.querySelector("#message").textContent="请求失败"}refresh()};
refresh();setInterval(refresh,5000);
</script></main></body></html>"""


CASES_PAGE = r"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Tester 用例脑图</title><style>
:root{color-scheme:light;--ink:#182a40;--muted:#62758a;--line:#d7e1ed;--blue:#356db3;--teal:#21877f;--violet:#7460aa;--red:#b7473a;--bg:#f4f7fb}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px system-ui,-apple-system,"Segoe UI",sans-serif}header{padding:22px 28px 18px;border-bottom:1px solid var(--line);background:#fff;display:flex;align-items:center;justify-content:space-between;gap:18px;flex-wrap:wrap}h1{font-size:25px;margin:0 0 5px}header p{color:var(--muted);margin:0}.tools{display:flex;gap:9px;align-items:center;flex-wrap:wrap}select,input,button{font:inherit;border:1px solid #bdcad8;border-radius:7px;background:#fff;color:var(--ink);padding:9px 11px}button{cursor:pointer}button:hover{border-color:var(--blue)}button:focus-visible,select:focus-visible,input:focus-visible,[tabindex]:focus-visible{outline:3px solid #75a6da;outline-offset:2px}main{max-width:1540px;margin:auto;padding:20px 24px}.summary{display:flex;align-items:center;gap:14px;flex-wrap:wrap;color:var(--muted);padding:4px 0 14px}.summary strong{color:var(--ink)}.layout{display:grid;grid-template-columns:minmax(0,1fr) 330px;gap:16px;align-items:start}.map-shell,.detail{background:#fff;border:1px solid var(--line);border-radius:10px}.map-toolbar{padding:10px 14px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center;color:var(--muted);gap:10px}.viewport{overflow:auto;max-height:calc(100vh - 230px);min-height:520px}.viewport svg{display:block;min-width:1100px}.edge{fill:none;stroke:#afc1d5;stroke-width:2}.edge.case{stroke:#bfd2d9}.node rect{fill:#fff;stroke:#b7c8d9;stroke-width:1.5;rx:9}.node.root rect{fill:#182a40;stroke:#182a40}.node.root text{fill:#fff}.node.module rect{fill:#edf4fb;stroke:#88a9cc}.node.case rect{fill:#f4f9f8;stroke:#9acac1}.node.case.todo rect{fill:#fff8e9;stroke:#e2c98d}.node text{pointer-events:none;font-family:inherit}.node .id{font-size:12px;font-weight:700;fill:#55718f}.node.root .id{fill:#c4d7eb}.node .label{font-size:14px;font-weight:650;fill:var(--ink)}.node .meta{font-size:11px;fill:var(--muted)}.node{cursor:pointer}.node:hover rect,.node.selected rect{stroke:var(--blue);stroke-width:3}.module rect{fill:#edf4fb}.module .label{font-size:15px}.detail{padding:18px;position:sticky;top:16px;max-height:calc(100vh - 32px);overflow:auto}.detail h2{font-size:19px;line-height:1.4;margin:0 0 6px}.detail .case-id{font:700 12px ui-monospace,monospace;color:var(--blue)}.badges{display:flex;gap:6px;flex-wrap:wrap;margin:12px 0 18px}.badge{padding:4px 8px;background:#eef3f8;border-radius:5px;font-size:12px;color:#455e77}.badge.todo{background:#fff2d7;color:#745214}.detail h3{font-size:13px;margin:18px 0 6px;color:#3e5872}.detail p,.detail ol{font-size:14px;line-height:1.65;color:#344960;white-space:pre-wrap}.detail ol{padding-left:22px}.empty{padding:34px;color:var(--muted);text-align:center}.legend{display:flex;gap:12px;font-size:12px}.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:5px;background:#88a9cc}.dot.todo{background:#e2c98d}@media(max-width:900px){main{padding:14px}.layout{grid-template-columns:1fr}.detail{position:static;max-height:none}.viewport{min-height:440px;max-height:62vh}header{padding:16px}.summary{gap:8px}}@media(prefers-reduced-motion:reduce){*{scroll-behavior:auto!important;transition:none!important}}
</style></head><body><header><div><h1>测试用例脑图</h1><p>从项目用例文档实时整理。点击模块展开用例，再选一条查看详细条件和步骤。</p></div><div class="tools"><label for="project">项目</label><select id="project"></select><input id="search" type="search" placeholder="搜索编号或用例名称" aria-label="搜索编号或用例名称"><button id="zoom-out" type="button" aria-label="缩小脑图">−</button><button id="zoom-in" type="button" aria-label="放大脑图">+</button><a href="architecture">项目依赖图</a><a href="/report/control/">返回测试控制台</a></div></header>
<main><div class="summary" id="summary"></div><div class="layout"><section class="map-shell"><div class="map-toolbar"><span>项目 → 功能模块 → 测试用例</span><span class="legend"><span><i class="dot"></i>已实现</span><span><i class="dot todo"></i>待实现 / 待确认</span></span></div><div class="viewport" id="viewport"><svg id="map" viewBox="0 0 1200 600" role="img" aria-label="测试用例层级脑图"></svg></div></section><aside class="detail" id="detail"><div class="empty">选择一个用例查看详细信息。</div></aside></div></main>
<script>
const svg=document.querySelector('#map'),projectPicker=document.querySelector('#project'),search=document.querySelector('#search'),detail=document.querySelector('#detail'),summary=document.querySelector('#summary');let projects=[],expanded=new Set(),selected=null,zoom=1;
const ns='http://www.w3.org/2000/svg',make=(tag,attrs={})=>{const el=document.createElementNS(ns,tag);for(const [key,value]of Object.entries(attrs))el.setAttribute(key,value);return el},text=(parent,x,y,value,cls)=>{const el=make('text',{x,y,class:cls});el.textContent=value;parent.append(el);return el};
function current(){return projects.find(p=>p.id===projectPicker.value)}
function render(){const p=current();if(!p)return;const query=search.value.trim().toLowerCase();const modules=p.modules.map(m=>({...m,cases:m.cases.filter(c=>!query||(c.id+' '+c.title+' '+m.name).toLowerCase().includes(query))})).filter(m=>m.cases.length);if(query)modules.forEach(m=>expanded.add(m.id));let y=42;const blocks=modules.map(m=>{const open=expanded.has(m.id),visible=open?m.cases:[],height=86+visible.length*102,start=y;y+=height+26;return{module:m,open,start,height,visible}});const height=Math.max(600,y+25);svg.setAttribute('viewBox',`0 0 ${1200/zoom} ${height/zoom}`);svg.style.width='1200px';svg.style.height=height+'px';svg.replaceChildren();const rootY=height/2;for(const b of blocks){const cy=b.start+b.height/2;svg.append(make('path',{d:`M204 ${rootY} C246 ${rootY},246 ${cy},270 ${cy}`,class:'edge'}));if(b.open&&b.visible.length){const top=b.start+86,first=top+35,last=top+(b.visible.length-1)*102+35,middle=(first+last)/2;svg.append(make('path',{d:`M550 ${cy} C610 ${cy},610 ${middle},675 ${middle}`,class:'edge case'}));for(let i=0;i<b.visible.length;i++){const item=b.visible[i],cyc=first+i*102;svg.append(make('path',{d:`M640 ${middle} C658 ${middle},658 ${cyc},675 ${cyc}`,class:'edge case'}));drawCase(item,675,cyc-36)}}drawModule(b.module,270,cy,b.open)}drawRoot(p,24,rootY-42);summary.replaceChildren();const done=p.modules.flatMap(m=>m.cases).filter(c=>c.status.includes('✅')).length;const values=[[p.name,p.case_count+' 条用例'],[modules.length+' 个功能模块',null],[done+' 条已标记实现',null]];for(const [label,value]of values){const item=document.createElement('span');if(value){const strong=document.createElement('strong');strong.textContent=label+'　'+value;item.append(strong)}else item.textContent=label;summary.append(item)}if(selected){const found=p.modules.flatMap(m=>m.cases).find(c=>c.id===selected);if(found)showCase(found)}}
function drawRoot(p,x,y){const g=make('g',{class:'node root'});g.append(make('rect',{x,y,width:180,height:84}));text(g,x+14,y+25,p.id,'id');text(g,x+14,y+49,p.name.replace(/ —.*|—.*/,''),'label');text(g,x+14,y+68,p.case_count+' 条测试用例','meta');svg.append(g)}
function drawModule(m,x,center,open){const y=center-36,g=make('g',{class:'node module',tabindex:'0',role:'button','aria-expanded':String(open)});g.append(make('rect',{x,y,width:280,height:72}));text(g,x+14,y+25,m.name,'label');text(g,x+14,y+49,m.cases.length+' 条用例 · '+(open?'点击收起':'点击展开'),'meta');g.addEventListener('click',()=>{expanded.has(m.id)?expanded.delete(m.id):expanded.add(m.id);render()});g.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();g.dispatchEvent(new MouseEvent('click'))}});svg.append(g)}
function drawCase(c,x,y){const todo=!c.status.includes('✅'),g=make('g',{class:'node case'+(todo?' todo':'')+(selected===c.id?' selected':''),tabindex:'0',role:'button'});g.append(make('rect',{x,y,width:480,height:76}));text(g,x+13,y+19,c.id,'id');const title=c.title.length>48?c.title.slice(0,47)+'…':c.title;text(g,x+13,y+41,title,'label');text(g,x+13,y+61,[c.priority,c.kind,c.status.includes('✅')?'已实现':'待实现/确认'].filter(Boolean).join('　'),'meta');g.addEventListener('click',()=>{selected=c.id;showCase(c);render()});g.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();g.dispatchEvent(new MouseEvent('click'))}});svg.append(g)}
function showCase(c){detail.replaceChildren();const id=document.createElement('div');id.className='case-id';id.textContent=c.id;detail.append(id);const h=document.createElement('h2');h.textContent=c.title;detail.append(h);const badges=document.createElement('div');badges.className='badges';for(const value of [c.priority,c.kind,c.status.includes('✅')?'已实现':'待实现 / 待确认'])if(value){const b=document.createElement('span');b.className='badge'+(value==='待实现 / 待确认'?' todo':'');b.textContent=value;badges.append(b)}detail.append(badges);for(const [label,key]of [['前置条件','preconditions'],['操作步骤','steps'],['预期结果','expected']]){if(!c[key])continue;const heading=document.createElement('h3');heading.textContent=label;detail.append(heading);if(key==='steps'){const ol=document.createElement('ol');for(const item of c.steps.split(/\n/).filter(Boolean)){const li=document.createElement('li');li.textContent=item.replace(/^\d+[.)、]\s*/, '');ol.append(li)}detail.append(ol)}else{const p=document.createElement('p');p.textContent=c[key];detail.append(p)}}}
document.querySelector('#zoom-in').onclick=()=>{zoom=Math.min(1.5,zoom+.1);render()};document.querySelector('#zoom-out').onclick=()=>{zoom=Math.max(.65,zoom-.1);render()};search.addEventListener('input',render);projectPicker.addEventListener('change',()=>{expanded.clear();selected=null;detail.innerHTML='<div class="empty">选择一个用例查看详细信息。</div>';render()});
fetch('api/cases',{cache:'no-store'}).then(async response=>{if(response.status===401){location.href='/report/control/';throw new Error('login required')}if(!response.ok)throw new Error('用例目录暂时不可用');projects=await response.json();for(const p of projects){const option=document.createElement('option');option.value=p.id;option.textContent=p.name;projectPicker.append(option)}render()}).catch(error=>{if(error.message!=='login required'){summary.textContent=error.message;svg.replaceChildren()}});
</script></body></html>"""


TOPOLOGY_PAGE = r"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>项目与依赖关系</title><style>
:root{color-scheme:light;--ink:#182a40;--muted:#64758a;--line:#d8e2ed;--bg:#f4f7fb;--blue:#356db3;--teal:#21877f;--violet:#7863ad;--gold:#a8751d}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px system-ui,-apple-system,"Segoe UI",sans-serif}header{background:#fff;border-bottom:1px solid var(--line);padding:22px 28px;display:flex;justify-content:space-between;align-items:center;gap:16px;flex-wrap:wrap}h1{font-size:25px;margin:0 0 5px}header p{margin:0;color:var(--muted)}.tools{display:flex;align-items:center;gap:10px;flex-wrap:wrap}input,select,button{font:inherit;padding:9px 11px;border:1px solid #bdcad8;border-radius:7px;background:#fff;color:var(--ink)}button{cursor:pointer}button:hover{border-color:var(--blue)}:focus-visible{outline:3px solid #75a6da;outline-offset:2px}a{color:var(--blue)}main{padding:18px 24px;max-width:1700px;margin:auto}.legend,.summary{display:flex;gap:14px;align-items:center;flex-wrap:wrap;color:var(--muted);padding:8px 0 14px}.legend span{display:flex;align-items:center;gap:6px}.swatch{width:10px;height:10px;border-radius:2px;background:var(--blue)}.swatch.service{background:var(--teal)}.swatch.dependency{background:var(--violet)}.swatch.optional{border:1px dashed var(--gold);background:white}.map-shell{background:white;border:1px solid var(--line);border-radius:10px;overflow:hidden}.columns{display:grid;grid-template-columns:repeat(4,1fr);min-width:1440px;background:#f9fbfd;border-bottom:1px solid var(--line)}.columns span{padding:11px 14px;font-weight:650;color:#3d5874;border-right:1px solid #e6edf4}.viewport{overflow:auto;max-height:calc(100vh - 255px);min-height:540px}.viewport svg{display:block;min-width:1510px}.edge{fill:none;stroke:#aec0d4;stroke-width:1.7;marker-end:url(#arrow)}.edge.optional{stroke-dasharray:6 5;stroke:#c3a76d}.edge.related{stroke:#7bb5aa}.node{cursor:pointer}.node rect{fill:#fff;stroke:#b9c9d9;stroke-width:1.5;rx:8}.node.suite rect{fill:#edf4fb;stroke:#89a9cc}.node.service rect{fill:#eff8f6;stroke:#8abeb4}.node.dependency rect{fill:#f5f2fb;stroke:#b5a6d2}.node.selected rect,.node:hover rect{stroke:#2267ad;stroke-width:3}.node.filtered{opacity:.2}.node .title{font-size:14px;font-weight:700;fill:var(--ink)}.node .kind{font-size:11px;fill:#60758c}.node .id{font-size:10px;fill:#52749b}.node text{pointer-events:none;font-family:inherit}.detail{margin-top:14px;background:white;border:1px solid var(--line);border-radius:9px;padding:16px 18px;min-height:86px}.detail strong{display:block;margin-bottom:5px}.detail p{margin:0;color:#445b73;line-height:1.6}.empty{color:var(--muted)}@media(max-width:740px){header{padding:16px}main{padding:12px}.viewport{min-height:470px;max-height:60vh}}
</style></head><body><header><div><h1>项目与依赖关系</h1><p>查看 E2E 项目调用哪些产品服务，以及服务需要哪些共享中间件和软件。</p></div><div class="tools"><input id="search" type="search" placeholder="搜索项目、服务或中间件" aria-label="搜索项目、服务或中间件"><a href="/report/control/cases">测试用例脑图</a><a href="/report/control/">Tester 控制台</a></div></header>
<main><div class="summary" id="summary"></div><div class="legend"><span><i class="swatch"></i>测试项目</span><span><i class="swatch service"></i>产品服务 / 仓库</span><span><i class="swatch dependency"></i>中间件与外部软件</span><span><i class="swatch optional"></i>可选 / 特定场景依赖</span><span>箭头由使用方指向依赖方</span></div><section class="map-shell"><div class="columns"><span>Tester 运行器</span><span>Playwright 项目</span><span>产品服务 / 仓库</span><span>共享中间件与软件</span></div><div id="viewport" class="viewport"><svg id="map" viewBox="0 0 1510 600" role="img" aria-label="Tester 项目及依赖关系图"></svg></div></section><aside id="detail" class="detail"><span class="empty">选择一个节点，查看它的职责与关系。</span></aside></main>
<script>
const svg=document.querySelector('#map'),detail=document.querySelector('#detail'),search=document.querySelector('#search'),summary=document.querySelector('#summary'),ns='http://www.w3.org/2000/svg';let topology=null,selected='';const cols={runner:25,suites:390,services:770,dependencies:1170},widths={runner:255,suites:255,services:285,dependencies:285};
function el(tag,attrs={}){const node=document.createElementNS(ns,tag);for(const [key,value]of Object.entries(attrs))node.setAttribute(key,value);return node}function label(parent,x,y,value,cls){const node=el('text',{x,y,class:cls});node.textContent=value;parent.append(node)}
function render(){if(!topology)return;const query=search.value.trim().toLowerCase(),nodes=topology.nodes,edges=topology.edges,visible=new Set(nodes.filter(node=>!query||(node.name+' '+node.kind+' '+node.note).toLowerCase().includes(query)).map(node=>node.id));if(query){for(const edge of edges)if(visible.has(edge.from)||visible.has(edge.to)){visible.add(edge.from);visible.add(edge.to)}}const byLayer={};for(const node of nodes){(byLayer[node.layer]??=[]).push(node)}let maxRows=0;for(const values of Object.values(byLayer))maxRows=Math.max(maxRows,values.length);const height=Math.max(600,55+maxRows*96),drawWidth=Math.max(1510,document.querySelector('#viewport').clientWidth);svg.setAttribute('viewBox',`0 0 1510 ${height}`);svg.style.width=drawWidth+'px';svg.style.height=(height*drawWidth/1510)+'px';svg.replaceChildren();const defs=el('defs'),marker=el('marker',{id:'arrow',viewBox:'0 0 10 10',refX:'9',refY:'5',markerWidth:'6',markerHeight:'6',orient:'auto-start-reverse'}),arrow=el('path',{d:'M 0 0 L 10 5 L 0 10 z',fill:'#91a7bf'});marker.append(arrow);defs.append(marker);svg.append(defs);for(const layer of topology.layers){label(svg,cols[layer.id],28,layer.name,'kind')}
const positions={};for(const layer of topology.layers){const values=byLayer[layer.id]||[];values.forEach((node,index)=>{positions[node.id]={x:cols[node.layer],y:50+index*96};})}
for(const edge of edges){if(!visible.has(edge.from)||!visible.has(edge.to))continue;const a=positions[edge.from],b=positions[edge.to];if(!a||!b)continue;const x1=a.x+widths[topology.nodes.find(item=>item.id===edge.from).layer],y1=a.y+31,x2=b.x,y2=b.y+31;const path=el('path',{d:`M${x1} ${y1} C${x1+38} ${y1},${x2-38} ${y2},${x2} ${y2}`,class:'edge'+(edge.optional?' optional':'')+(edge.from.startsWith('tc-')?' related':'')});path.append(el('title'));path.firstChild.textContent=edge.relation;svg.append(path)}
for(const node of nodes){const point=positions[node.id],x=point.x,y=point.y,category=node.layer==='suites'?'suite':node.layer==='dependencies'?'dependency':node.layer==='services'?'service':'',g=el('g',{class:`node ${category} ${selected===node.id?'selected':''} ${visible.has(node.id)?'':'filtered'}`,tabindex:'0',role:'button','aria-label':`${node.name}, ${node.kind}`});g.append(el('rect',{x,y,width:widths[node.layer],height:64}));label(g,x+11,y+19,node.name,'title');label(g,x+11,y+37,node.kind.length>40?node.kind.slice(0,38)+'…':node.kind,'kind');label(g,x+11,y+54,node.id,'id');g.addEventListener('click',()=>showNode(node));g.addEventListener('keydown',event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();showNode(node)}});svg.append(g)}
summary.replaceChildren();const projectCount=nodes.filter(node=>node.layer==='suites').length,serviceCount=nodes.filter(node=>node.layer==='services').length,dependencyCount=nodes.filter(node=>node.layer==='dependencies').length;summary.textContent=`${projectCount} 个 E2E 项目　${serviceCount} 个产品服务 / 仓库　${dependencyCount} 个中间件或外部软件　${edges.length} 条关系`}
function showNode(node){selected=node.id;render();const related=topology.edges.filter(edge=>edge.from===node.id||edge.to===node.id);detail.replaceChildren();const strong=document.createElement('strong');strong.textContent=`${node.name} · ${node.kind}`;const p=document.createElement('p');p.textContent=node.note+(related.length?'　关系：'+related.map(edge=>`${edge.from===node.id?'使用':'被 '+topology.nodes.find(item=>item.id===edge.from)?.name+' 使用'}（${edge.relation}${edge.optional?'，可选':''}）`).join('；'):'');detail.append(strong,p)}
search.addEventListener('input',render);window.addEventListener('resize',render);fetch('api/architecture',{cache:'no-store'}).then(async response=>{if(!response.ok)throw new Error('关系图数据读取失败');topology=await response.json();render()}).catch(error=>{summary.textContent=error.message});
</script></body></html>"""


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
        self.send_header("Set-Cookie", f"{SESSION_COOKIE}=; Path=/report/control/; Max-Age=0; HttpOnly; Secure; SameSite=Lax")

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
        if path == "/":
            self.reply(200, PAGE.encode(), "text/html; charset=utf-8")
            return
        if path == "/cases":
            self.reply(200, CASES_PAGE.encode(), "text/html; charset=utf-8")
            return
        if path == "/architecture":
            self.reply(200, TOPOLOGY_PAGE.encode(), "text/html; charset=utf-8")
            return
        if path == "/api/architecture":
            topology = json.loads((REPO / "docs/project-dependency-map.json").read_text(encoding="utf-8"))
            self.json_reply(200, topology)
            return
        if path == "/web3-bs.js":
            sdk = REPO / "node_modules/@yeying-community/web3-bs/dist/web3-bs.umd.js"
            try:
                self.reply(200, sdk.read_bytes(), "application/javascript; charset=utf-8")
            except OSError:
                self.json_reply(503, {"error": "Wallet login component is unavailable"})
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
                    "Set-Cookie": f"{SESSION_COOKIE}={token}; Path=/report/control/; Max-Age={SESSION_TTL_SECONDS}; HttpOnly; Secure; SameSite=Lax"
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

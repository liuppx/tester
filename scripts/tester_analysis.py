"""Read-only analysis of Tester case catalogs and private runner evidence."""

from collections import Counter, defaultdict
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
from xml.etree import ElementTree

from local_test_readiness import read_env


STAMP_FORMAT = "%Y-%m-%d_%H-%M-%S%z"
RUN_ID = re.compile(r"\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}[+-]\d{4}")
CASE_ID = re.compile(r"\b[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*-\d{3}\b")
SPEC = re.compile(r"products/([a-z][a-z0-9-]*)/tests/[\w./-]+\.spec\.ts")
TOKEN = re.compile(r"yt_[A-Za-z0-9_-]{43}")
FULL = "__full__"
EXECUTED_OUTCOMES = ("expected", "unexpected", "flaky")
OUTCOMES = (*EXECUTED_OUTCOMES, "expected-failure", "flaky-expected-failure", "skipped", "interrupted", "not-run")
ADDITIONAL_CONFIG_KEYS = {
    "chat": {"OPENAI_API_KEY", "ANTHROPIC_API_KEY"},
    "node": {"MAILPIT_URL"},
    "project": {"APP_DEV_PORT"},
}
MAX_REPORT_BYTES = 16 * 1024 * 1024


def token_digest(token):
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def read_evidence(path, parser=json.loads):
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_REPORT_BYTES:
            return None
        with path.open("rb") as stream:
            raw = stream.read(MAX_REPORT_BYTES + 1)
            return parser(raw) if len(raw) <= MAX_REPORT_BYTES else None
    except (OSError, ValueError, ElementTree.ParseError):
        return None


def read_object(path):
    data = read_evidence(path)
    return data if isinstance(data, dict) else {}


def config_keys(repo, project, dependencies):
    template = (repo / "tester.env.template").read_text(encoding="utf-8")
    known = set(re.findall(r"^\s*(?:#\s*)?([A-Z][A-Z0-9_]+)=", template, re.MULTILINE))
    prefixes = (project.upper() + "_", "BOOKS_") if project == "book" else (project.upper() + "_",)
    service_keys = {item["environment_key"] for item in dependencies["services"] if item["environment_key"]}
    return sorted({key for key in known if key.startswith(prefixes)}
                  | service_keys | ADDITIONAL_CONFIG_KEYS.get(project, set()))


def project_dependencies(repo, name):
    manifest = read_object(repo / "local-test-dependencies.json")
    definition = manifest.get("projects", {}).get(name, {})
    services = manifest.get("services", {})
    return {
        "repositories": definition.get("repositories", []),
        "services": [
            {"name": service, "kind": services[service].get("kind", "product-service"),
             "environment_key": services[service].get("url_key"),
             "has_local_default": "default_port" in services[service] or "port" in services[service]}
            for service in definition.get("services", []) if service in services
        ],
        "in_full_run": name in manifest.get("full_run_projects", []),
    }


def validated_commit(value):
    return value if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value) else None


def project_configuration(repo, name, watcher, local_source):
    dependencies = project_dependencies(repo, name)
    # The managed runner selects TEST_ENV=local. Local-source launchers can
    # still supply overrides, which appear in the per-batch snapshot.
    configured = os.environ.get("TESTER_ENV_FILE", "").strip()
    env_file = repo / configured if configured else repo / ".env.local"
    try:
        values = read_env(env_file)
        readable = True
    except (OSError, ValueError):
        values, readable = {}, False
    return {
        "watcher_enabled": bool(watcher.get("enabled")),
        "mode": "local-source" if local_source else "deployed",
        "included_in_full_run": dependencies["in_full_run"] and (local_source or name != "knowledge"),
        "dependencies": dependencies,
        "environment_file_readable": readable,
        "environment_variables": [
            {"name": key, "configured": bool(os.environ.get(key, values.get(key, "")).strip())}
            for key in config_keys(repo, name, dependencies)
        ],
    }


def load_run_evidence(private, public):
    if private.is_symlink() or public.is_symlink():
        return {}, {}, {}, None
    batch = read_object(private / "batch.json")
    summary = read_object(public / "summary.json")
    execution = read_object(private / "execution.json")
    junit = None if execution else read_evidence(private / "junit.xml", ElementTree.fromstring)
    return batch, summary, execution, junit


def junit_cases(root, project, scope):
    if root is None:
        return []
    cases = []
    for suite in root.iter("testsuite"):
        for case in suite.findall("testcase"):
            file = suite.get("name", "")
            if not SPEC.fullmatch(file):
                file = case.get("classname", "")
            match = SPEC.fullmatch(file)
            if (match and match[1] != project) or (not match and scope != project):
                continue
            title = case.get("name", "")
            identity = "\0".join((project, file, case.get("classname", ""), title))
            outcome = "expected"
            if case.find("skipped") is not None:
                outcome = "skipped"
            elif case.find("failure") is not None or case.find("error") is not None:
                outcome = "unexpected"
            try:
                duration = max(0, round(float(case.attrib["time"]) * 1000))
            except (KeyError, ValueError, OverflowError):
                duration = None
            cases.append({
                "id": hashlib.sha256(identity.encode()).hexdigest()[:32],
                "case_ids": sorted(set(CASE_ID.findall(title))),
                "file": file if match else None,
                "outcome": outcome,
                "duration_ms": duration,
                "attempts": None,
            })
    return cases


def safe_cases(execution, project):
    cases = []
    for case in execution.get("cases", []):
        if case.get("project") != project or case.get("outcome") not in OUTCOMES:
            continue
        file = case.get("file", "")
        match = SPEC.fullmatch(file)
        if not match or match[1] != project:
            continue
        attempts = [
            {key: attempt.get(key) for key in ("retry", "status", "duration_ms", "failure_location")}
            for attempt in case.get("attempts", [])
        ]
        cases.append({
            "id": case["id"], "case_ids": case["case_ids"], "file": file, "line": case["line"],
            "outcome": case["outcome"], "expected_status": case["expected_status"],
            "timeout_ms": case["timeout_ms"], "attempts": attempts,
            "duration_ms": sum(attempt["duration_ms"] for attempt in attempts) if attempts else None,
        })
    return cases


class RunHistory:
    def __init__(self, private, public, project, repo):
        self.private = private
        self.public = public
        self.project = project
        self.env_keys = config_keys(repo, project, project_dependencies(repo, project))

    def directories(self, scope):
        if scope == FULL:
            return self.private, self.public / "runs"
        return self.private / "projects" / scope, self.public / "projects" / scope / "runs"

    def detail(self, scope, run, full_evidence=None):
        private, public = self.directories(scope)
        private, public = private / run, public / run
        if full_evidence is None:
            batch, summary, execution, junit = load_run_evidence(private, public)
        else:
            batch, summary, execution, junit = full_evidence
        if batch.get("run") != run and summary.get("run") != run:
            return None
        cases = safe_cases(execution, self.project) if execution else junit_cases(junit, self.project, scope)
        if scope == FULL and not cases and self.project not in batch.get("planned_projects", []):
            return None  # No evidence that this project was selected.
        try:
            started = datetime.strptime(run, STAMP_FORMAT)
        except ValueError:
            return None
        source_commits = read_object(private / "source-commits.json").get("source_commits") or summary.get("source_commits", {})
        commits = {name: sha for name, sha in source_commits.items()
                   if validated_commit(sha)}
        mode = batch.get("mode") or ("local-source" if commits else "unknown")
        config = execution.get("config", {})
        configured = config.get("configured")
        if isinstance(configured, dict):
            configured = {key: configured[key] for key in self.env_keys if type(configured.get(key)) is bool}
        else:
            configured = None
        evidence = "playwright" if execution else "junit" if cases else "summary" if summary else "metadata"
        counts = Counter(case["outcome"] for case in cases)
        project_config = config.get("projects", {}).get(self.project)
        runner_status = execution.get("status")
        stage = batch.get("stage", "published")
        tested_commit = validated_commit(batch.get("target_sha") or summary.get("target_sha"))
        tester_commit = validated_commit(batch.get("tester_sha") or summary.get("tester_sha"))
        report_path = f"runs/{run}" if scope == FULL else f"projects/{scope}/runs/{run}"
        return {
            "run": run, "scope": scope, "project": self.project, "mode": mode,
            "stage": stage, "evidence": evidence, "runner_status": runner_status,
            "summary_status": summary.get("status"),
            "started_at": started.isoformat(), "finished_at": (batch.get("finished_at") or summary.get("finished_at"))
            if stage != "running" else None,
            "report": f"/report/{report_path}/index.html" if summary else None,
            "counts": {key: counts[key] for key in OUTCOMES} if cases else None,
            "published_counts": {key: summary.get(key) for key in ("tests", "passed", "failed", "skipped")}
            if scope != FULL and summary else None,
            "config": {
                "test_env": batch.get("test_env"),
                "trigger": batch.get("trigger") or summary.get("trigger"),
                "tester_commit": tester_commit,
                "target_commit": commits.get(self.project) or (tested_commit if scope != FULL else None),
                "source_commits": commits,
                "workers": config.get("workers"),
                "playwright_version": config.get("playwright_version"),
                "project": project_config,
                "environment_variables": configured,
            },
            "cases": cases,
        }

    def list(self, limit, before=None, mode=None):
        candidates = []
        for scope in (self.project, FULL):
            names = set()
            for directory in self.directories(scope):
                if directory.is_dir():
                    names.update(path.name for path in directory.iterdir() if RUN_ID.fullmatch(path.name))
            for run in names:
                try:
                    key = datetime.strptime(run, STAMP_FORMAT).timestamp(), run, scope
                except ValueError:
                    continue
                if before is None or key < before:
                    candidates.append(key)
        result = []
        for _, run, scope in sorted(candidates, reverse=True):
            record = self.detail(scope, run)
            if record is not None and (mode is None or record["mode"] == mode):
                result.append(record)
                if len(result) == limit:
                    break
        return result


def stability(runs):
    groups = defaultdict(lambda: {"batches": 0, "batches_with_cases": 0, "counts": Counter(), "cases": {}})
    for run in runs:
        group = groups[run["mode"], run["evidence"]]
        group["batches"] += 1
        group["batches_with_cases"] += bool(run["cases"])
        for case in run["cases"]:
            outcome = case["outcome"]
            group["counts"][outcome] += 1
            row = group["cases"].setdefault(case["id"], {
                "id": case["id"], "case_ids": case["case_ids"], "file": case["file"],
                "outcomes": Counter(), "durations": [],
            })
            row["case_ids"] = sorted(set(row["case_ids"]) | set(case["case_ids"]))
            row["outcomes"][outcome] += 1
            if case["duration_ms"] is not None and outcome in EXECUTED_OUTCOMES:
                row["durations"].append(case["duration_ms"])
    results = []
    for (mode, evidence), group in sorted(groups.items()):
        cases = []
        for row in group.pop("cases").values():
            outcomes = row["outcomes"]
            executions = sum(outcomes[key] for key in EXECUTED_OUTCOMES)
            durations = sorted(row.pop("durations"))
            mixed = bool(outcomes["unexpected"] and (outcomes["expected"] or outcomes["flaky"]))
            cases.append({
                **row, "executions": executions,
                "mixed_final_outcomes": mixed if executions >= 2 else None,
                "flaky_rate": outcomes["flaky"] / executions if executions and evidence == "playwright" else None,
                "p95_duration_ms": durations[(95 * len(durations) + 99) // 100 - 1] if durations else None,
            })
        cases.sort(key=lambda row: (not row["mixed_final_outcomes"], -row["outcomes"]["flaky"], row["id"]))
        results.append({"mode": mode, "evidence": evidence, **group, "cases": cases})
    return results

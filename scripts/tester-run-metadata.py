"""Record the stage reached by a scheduled Tester batch, including preflight failures."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys


def private_open(path, flags):
    return os.open(path, flags, 0o600)


def main():
    directory, run, project, stage = sys.argv[1:]
    temporary = Path(directory) / ".batch.json.tmp"
    local_source = os.environ["TESTER_LOCAL_SOURCE_MODE"] == "1"
    runner = Path(os.environ.get("TESTER_SOURCE_DIR") or Path(__file__).resolve().parents[1])
    manifest = runner / "local-test-dependencies.json"
    planned_projects = [project] if project else []
    if not project and manifest.is_file():
        projects = json.loads(manifest.read_text(encoding="utf-8"))["full_run_projects"]
        planned_projects = [name for name in projects if name != "knowledge" or local_source]
    data = {
        "run": run,
        "project": project or "__full__",
        "batch_id": os.environ.get("TESTER_BATCH_ID")
        if re.fullmatch(r"[0-9a-f]{24}", os.environ.get("TESTER_BATCH_ID", "")) else None,
        "batch_project": os.environ.get("TESTER_BATCH_PROJECT") or None,
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "stage": stage,
        "mode": "local-source" if local_source else "deployed",
        "planned_projects": planned_projects,
        "test_env": os.environ["TEST_ENV"],
        "trigger": os.environ.get("TESTER_TRIGGER", "manual"),
        "tester_sha": os.environ.get("TESTER_SOURCE_SHA"),
        "target_sha": os.environ.get("TESTER_TARGET_SHA"),
    }
    with open(temporary, "w", encoding="utf-8", opener=private_open) as stream:
        json.dump(data, stream)
    temporary.replace(Path(directory) / "batch.json")


if __name__ == "__main__":
    main()

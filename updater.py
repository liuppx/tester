#!/usr/bin/env python3
"""Tester-only artifact deployment. No source fetch/build or product deployment."""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

MANIFEST = re.compile(r"release-manifest-tester-v(\d+\.\d+\.\d+)-([0-9a-f]{7,40})\.json")
REGISTRY = "https://warehouse.tidukongjian.com/dav/personal/public_community/package"


class Refused(RuntimeError):
    """Only fixed, nonsecret messages may be used in this exception."""


class Busy(Refused):
    pass


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1048576), b""):
            h.update(block)
    return h.hexdigest()


def version(text):
    if not re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", text):
        raise Refused("invalid artifact version")
    return tuple(map(int, text.split(".")))


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".update-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def private_file(path):
    path = Path(path)
    stat = path.lstat()
    if path.is_symlink() or not path.is_file() or stat.st_uid != os.getuid() or stat.st_mode & 0o077:
        raise Refused("configuration file ownership or permissions rejected")
    return path


@contextmanager
def lock(path, shared=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise Refused("symlink lock rejected")
    with path.open("a") as stream:
        try:
            fcntl.flock(stream, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise Busy("test or deployment already active; update deferred") from error
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Registry:
    def __init__(self, credential_file, cache):
        values = {}
        for line in private_file(credential_file).read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, raw = line.removeprefix("export ").split("=", 1)
            words = shlex.split(raw, comments=True)
            if len(words) == 1:
                values[key.strip()] = words[0]
        self.url = values.get("WEBDAV_PACKAGE_BASE_URL", "").rstrip("/")
        if self.url != REGISTRY:
            raise Refused("unexpected package registry")
        ak, sk = values.get("WEBDAV_PACKAGE_AK"), values.get("WEBDAV_PACKAGE_SK")
        if not ak or not sk:
            raise Refused("registry credentials missing")
        self.authorization = "Basic " + base64.b64encode((ak + ":" + sk).encode()).decode()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        self.cache = Path(cache)
        self.cache.mkdir(parents=True, exist_ok=True, mode=0o700)

    def request(self, name, limit, method="GET", data=None):
        if name and (Path(name).name != name or not re.fullmatch(r"[a-zA-Z0-9._-]+", name)):
            raise Refused("invalid registry filename")
        headers = {"Authorization": self.authorization}
        if method == "PROPFIND":
            headers.update({"Depth": "1", "Content-Type": "application/xml"})
        request = urllib.request.Request(self.url + "/" + name, method=method, data=data, headers=headers)
        return self.opener.open(request, timeout=30)

    def manifests(self):
        with self.request("", 8 * 1024**2, "PROPFIND", b'<propfind xmlns="DAV:"><prop><getcontentlength/></prop></propfind>') as response:
            if response.status != 207:
                raise Refused("registry listing rejected")
            data = response.read(8 * 1024**2 + 1)
        if len(data) > 8 * 1024**2:
            raise Refused("registry listing too large")
        names = set()
        for href in ET.fromstring(data).iter("{DAV:}href"):
            name = urllib.parse.unquote(urllib.parse.urlsplit(href.text or "").path).rstrip("/").split("/")[-1]
            if MANIFEST.fullmatch(name):
                names.add(name)
        return sorted(names)

    def small(self, name, limit=1024**2):
        with self.request(name, limit) as response:
            data = response.read(limit + 1)
        if len(data) > limit:
            raise Refused("registry metadata too large")
        return data

    def archive(self, artifact):
        path = self.cache / artifact["package"]
        if path.exists():
            if path.is_symlink() or digest(path) != artifact["sha256"]:
                raise Refused("cached artifact checksum mismatch")
            return path
        temporary = path.with_name(path.name + ".partial")
        size, started = 0, time.monotonic()
        try:
            with self.request(path.name, 1024**3) as response, temporary.open("wb") as dest:
                while data := response.read(65536):
                    size += len(data)
                    if size > 1024**3 or time.monotonic() - started > 900:
                        raise Refused("artifact download size or time limit")
                    dest.write(data)
                dest.flush()
                os.fsync(dest.fileno())
            if digest(temporary) != artifact["sha256"]:
                raise Refused("downloaded artifact checksum mismatch")
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return path


def select_manifest(names, current, adopt=False):
    candidates = []
    for name in names:
        match = MANIFEST.fullmatch(name)
        if match and (version(match[1]) > version(current) or adopt and version(match[1]) == version(current)):
            candidates.append((version(match[1]), name))
    if not candidates:
        return None
    newest = max(row[0] for row in candidates)
    selected = [name for v, name in candidates if v == newest]
    if len(selected) != 1:
        raise Refused("multiple artifacts claim the latest version")
    return selected[0]


def validate_manifest(name, data):
    match = MANIFEST.fullmatch(name)
    obj = json.loads(data)
    if not match or obj.get("schema_version") != 1 or obj.get("repository") != "tester":
        raise Refused("invalid Tester manifest")
    sha = obj.get("source_commit", "")
    if not re.fullmatch("[0-9a-f]{40}", sha) or not sha.startswith(match[2]):
        raise Refused("manifest source identity mismatch")
    artifacts = [a for a in obj.get("artifacts", []) if a.get("unit_id") == "tester-service"]
    if len(artifacts) != 1:
        raise Refused("Tester service artifact missing or ambiguous")
    artifact = artifacts[0]
    root = f"tester-service-v{match[1]}-{match[2]}"
    expected = {"type": "service", "version": match[1], "package": root + ".tar.gz", "package_root": root,
                "service_target": {"module": "tester", "path": "/opt/deploy/tester"}}
    if any(artifact.get(k) != v for k, v in expected.items()) or not re.fullmatch("[0-9a-f]{64}", artifact.get("sha256", "")):
        raise Refused("artifact does not match Tester deployment contract")
    version(artifact["version"])
    return obj, artifact


def extract_checked(archive, artifact, folder):
    root = artifact["package_root"]
    names, size = set(), 0
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        if len(members) > 100000:
            raise Refused("too many archive entries")
        for member in members:
            name = PurePosixPath(member.name)
            if name.is_absolute() or ".." in name.parts or not name.parts or name.parts[0] != root or member.name in names:
                raise Refused("unsafe or duplicate archive path")
            if not (member.isfile() or member.isdir()) or member.mode & 0o6000:
                raise Refused("archive links or special entries rejected")
            if any(p in {".git", "private"} for p in name.parts) or (name.name.startswith(".env") and name.name != ".env.template"):
                raise Refused("private configuration in artifact rejected")
            size += member.size
            names.add(member.name)
        if size > 3 * 1024**3 or shutil.disk_usage(folder).free < size + 1024**3:
            raise Refused("archive expanded size or disk limit")
        # All links, traversal, special nodes and set-ID modes were rejected above.
        for member in members:
            destination = Path(folder) / member.name
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(member) as source, destination.open("xb") as output:
                    shutil.copyfileobj(source, output)
                os.chmod(destination, member.mode & 0o777)
    release = Path(folder) / root
    meta = json.loads((release / "release.json").read_text())
    if meta.get("unit_id") != "tester-service" or meta.get("version") != artifact["version"]:
        raise Refused("archive release metadata mismatch")
    for name in ("starter.sh", "health-check.sh", "run-tests.sh", "test.sh", "upgrade.sh", "tester-control.py"):
        if not (release / "scripts" / name).is_file():
            raise Refused("required package entry missing")
    return release


def apply_overlay(release, overlay):
    overlay = Path(overlay)
    manifest_path = overlay / "overlay.json"
    manifest = json.loads(manifest_path.read_text())
    results = []
    for row in manifest["files"]:
        name = row["path"]
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or path.parts[0] not in {"products", "scripts", "shared", "playwright.config.ts", "playwright.safe.config.ts", ".env.template"}:
            raise Refused("invalid adaptation path")
        target, content = Path(release) / name, overlay / "files" / name
        if digest(content) != row["target_sha256"]:
            raise Refused("adaptation content hash mismatch")
        current = digest(target) if target.is_file() else None
        if current == row["target_sha256"]:
            action = "already_present"
        elif current == row["base_sha256"]:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(content, target)
            action = "applied"
        elif row["base_sha256"] is None:
            raise Refused("upstream conflicts with a host-only adaptation")
        else:
            patch = (overlay / "patches" / row["patch"]).read_bytes()
            if hashlib.sha256(patch).hexdigest() != row["patch_sha256"]:
                raise Refused("adaptation patch hash mismatch")
            command = ["git", "apply", "--no-index", "--whitespace=nowarn"]
            p = subprocess.run([*command, "--check", "-"], input=patch, cwd=release, capture_output=True)
            if p.returncode:
                raise Refused("upstream adaptation conflict; current release retained")
            subprocess.run([*command, "-"], input=patch, cwd=release, check=True, capture_output=True)
            action = "merged"
        os.chmod(target, row["mode"])
        results.append({"path": name, "action": action, "installed_sha256": digest(target)})
    return {"overlay_sha256": digest(manifest_path), "base_source": manifest["base_source"], "files": results}


def command(args, *, cwd=None, env=None, timeout=60):
    # Never put environment values or child output in the public updater journal.
    p = subprocess.run(args, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
    if p.returncode:
        raise Refused("package hook, dependency or service check failed")
    return p.stdout


class Updater:
    def __init__(self, config):
        self.config = config
        self.state = Path(config["state_dir"])
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.link = Path(config["deploy_link"])
        self.deploy = self.link.parent.resolve()
        self.unit = config["unit"]
        self.runtime = Path(config["runtime_config"])
        self.values = json.loads(private_file(self.runtime).read_text())
        self.journal = self.state / "transaction.json"
        self.installed = self.state / "installed.json"
        self.gate = self.state / "activity.lock"

    def current(self):
        if not self.link.is_symlink():
            raise Refused("Tester stable path must be a symlink")
        path = self.link.resolve(strict=True)
        if path.parent != self.deploy or not path.name.startswith("tester-service-v"):
            raise Refused("unexpected active Tester release")
        return path

    def env(self):
        env = dict(os.environ)
        # Mirror service configuration without copying any secret into a release.
        env.update(self.values)
        env["TESTER_RUNTIME_CONFIG"] = str(self.runtime)
        env["TESTER_ARTIFACT_MODE"] = "1"
        env["TESTER_UPGRADE_GATE"] = str(self.gate)
        env["PATH"] = str(Path(env["TESTER_NODE_BIN"]).parent) + ":/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        return env

    def snapshot(self, phase, previous, target, **extra):
        value = {"phase": phase, "previous": str(previous), "target": str(target), "time": datetime.now(timezone.utc).isoformat(), **extra}
        atomic_json(self.journal, value)

    def switch(self, target):
        target = Path(target)
        if target.parent != self.deploy or not target.is_dir():
            raise Refused("invalid release switch target")
        temporary = self.link.with_name(".tester-link-" + str(os.getpid()))
        try:
            temporary.symlink_to(target)
            os.replace(temporary, self.link)
        finally:
            temporary.unlink(missing_ok=True)

    def health(self, release):
        env = self.env()
        command(["bash", str(release / "scripts/health-check.sh"), "--level", "all", "--config", str(self.runtime), "--timeout", "15"], env=env, timeout=45)
        command(["bash", str(release / "scripts/test.sh"), "--suite", "smoke", "--config", str(self.runtime), "--timeout", "15"], env=env, timeout=45)
        pid = int(command(["systemctl", "show", self.unit, "-p", "MainPID", "--value"]).strip())
        if not pid or Path(f"/proc/{pid}/cwd").resolve() != release:
            raise Refused("active service does not use candidate release")

    def busy_state(self):
        state = Path(self.values["TESTER_PRIVATE_REPORT_DIR"]) / "control/state.json"
        return state.exists() and bool(json.loads(state.read_text()).get("active"))

    def stop(self, bootstrap=False):
        if self.busy_state():
            raise Busy("control task active; update deferred")
        frozen = None
        try:
            if bootstrap:
                # First adoption predates the shared deployment gate. Freeze only
                # the owned control worker, then recheck its durable active state.
                request = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with request.open("http://127.0.0.1:" + self.values.get("TESTER_CONTROL_PORT", "18082") + "/healthz", timeout=3) as response:
                    health = json.load(response)
                pid = int(health["pid"])
                args = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
                if str(self.current() / "scripts/tester-control.py").encode() not in args:
                    raise Refused("bootstrap worker identity mismatch")
                os.kill(pid, signal.SIGSTOP)
                frozen = pid
                for _ in range(100):
                    if Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1][0] in "Tt":
                        break
                    time.sleep(0.01)
                else:
                    raise Refused("bootstrap worker did not pause")
                if self.busy_state():
                    raise Busy("control task began before bootstrap; update deferred")
                stop = subprocess.Popen(["systemctl", "stop", self.unit], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                # Default SIGTERM terminates the paused worker before it can
                # dequeue work; the supervisor and systemd perform normal cleanup.
                os.kill(pid, signal.SIGTERM)
                os.kill(pid, signal.SIGCONT)
                frozen = None
                if stop.wait(timeout=65):
                    raise Refused("control service did not stop")
            else:
                command(["systemctl", "stop", self.unit], timeout=65)
        finally:
            if frozen is not None:
                try:
                    os.kill(frozen, signal.SIGCONT)
                except ProcessLookupError:
                    pass

    def hook(self, phase, previous, target):
        command(["bash", str(target / "scripts/upgrade.sh"), phase, str(previous), str(target)], env=self.env(), timeout=90)

    def sandbox(self, target):
        with tempfile.TemporaryDirectory(prefix="acceptance-", dir=self.state) as name:
            folder = Path(name)
            projects = json.loads(Path(self.values["TESTER_CONTROL_CONFIG"]).read_text())
            projects["projects"] = {k: dict(v, enabled=False) for k, v in projects["projects"].items()}
            atomic_json(folder / "projects.json", projects)
            values = dict(self.values, TESTER_CONTROL_PORT=self.config["staging_port"], TESTER_CONTROL_CONFIG=str(folder / "projects.json"),
                          TESTER_PRIVATE_REPORT_DIR=str(folder / "private"), TESTER_REPORT_DIR=str(folder / "reports"),
                          TESTER_PID_FILE=str(folder / "pid"), TESTER_CONTROL_LOG=str(folder / "control.log"),
                          TESTER_RUNTIME_DIR=str(folder / "runtime"), TESTER_WATCH_UPSTREAM_MAIN="0", TESTER_UPGRADE_GATE=str(self.gate))
            atomic_json(folder / "runtime.json", values)
            env = dict(self.env(), **values, TESTER_RUNTIME_CONFIG=str(folder / "runtime.json"))
            started = False
            try:
                command(["bash", str(target / "scripts/starter.sh"), "start"], env=env, timeout=35)
                started = True
                for entry, options in [("health-check.sh", ["--level", "all"]), ("test.sh", ["--suite", "smoke"])]:
                    command(["bash", str(target / "scripts" / entry), *options, "--config", str(folder / "runtime.json"), "--timeout", "15"], env=env, timeout=45)
                command(["bash", str(target / "scripts/test.sh"), "--suite", "e2e", "--project", "books", "--list-cases", "--config", str(folder / "runtime.json"), "--timeout", "40"], env=env, timeout=55)
            finally:
                if started or (folder / "pid").exists():
                    command(["bash", str(target / "scripts/starter.sh"), "stop"], env=env, timeout=65)

    def stage(self, archive, manifest, artifact):
        target = self.deploy / artifact["package_root"]
        overlay_sha = digest(Path(self.config["overlay"]) / "overlay.json")
        if target.exists():
            marker = target / "host-adaptations.json"
            if marker.is_file() and json.loads(marker.read_text()).get("overlay_sha256") != overlay_sha:
                # Preserve an existing deployment unchanged when only the
                # separately maintained host adaptation gets a new revision.
                target = self.deploy / (artifact["package_root"] + "-host" + overlay_sha[:12])
        if target.exists():
            marker = target / "host-adaptations.json"
            if not marker.is_file():
                raise Refused("existing target has no adaptation provenance")
            provenance = json.loads(marker.read_text())
            if provenance.get("archive_sha256") != artifact["sha256"] or provenance.get("overlay_sha256") != overlay_sha:
                raise Refused("existing release differs from the candidate")
            if any(digest(target / row["path"]) != row["installed_sha256"] for row in provenance["files"]):
                raise Refused("existing adaptation files differ from their provenance")
            return target
        with tempfile.TemporaryDirectory(prefix=".tester-stage-", dir=self.deploy) as name:
            release = extract_checked(archive, artifact, name)
            metadata = json.loads((release / "release.json").read_text())
            if metadata.get("source_commit") != manifest["source_commit"]:
                raise Refused("package source differs from published manifest")
            provenance = apply_overlay(release, self.config["overlay"])
            provenance.update({"archive_sha256": artifact["sha256"], "upstream_source": manifest["source_commit"], "version": artifact["version"]})
            atomic_json(release / "host-adaptations.json", provenance)
            self.hook("prepare", self.current(), release)
            self.sandbox(release)
            os.rename(release, target)
        return target

    def backup(self, previous, target):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
        folder = Path(self.config["backup_dir"]) / stamp
        folder.mkdir(parents=True, mode=0o700)
        for source in (self.runtime, Path(self.values["TESTER_ENV_FILE"]), Path(self.values["TESTER_CONTROL_CONFIG"])):
            shutil.copyfile(source, folder / source.name)
            os.chmod(folder / source.name, 0o600)
        atomic_json(folder / "deployment.json", {"previous": str(previous), "target": str(target),
                    "unit": self.unit, "configuration_hashes": {p.name: digest(p) for p in folder.iterdir() if p.is_file()}})
        return folder

    def rollback(self, previous, target):
        command(["systemctl", "stop", self.unit], timeout=65)
        self.switch(previous)
        command(["systemctl", "start", self.unit], timeout=65)
        self.health(previous)
        self.snapshot("rolled_back", previous, target)

    def recover(self):
        if not self.journal.exists():
            return
        old = json.loads(self.journal.read_text())
        if old["phase"] in {"completed", "rolled_back", "deferred"}:
            return
        previous, target = Path(old["previous"]), Path(old["target"])
        if previous.parent != self.deploy or target.parent != self.deploy or self.current() not in (previous, target):
            raise Refused("interrupted transaction requires manual review")
        with lock(self.gate), lock(self.values["TESTER_LOCK_FILE"]):
            if self.busy_state():
                raise Busy("interrupted update waits for current task")
            # An interrupted transaction is conservatively restored, never
            # silently accepted from only a half-written successful journal.
            self.rollback(previous, target)

    def activate(self, target, bootstrap=False):
        previous = self.current()
        if target == previous:
            self.health(target)
            return "up_to_date"
        with lock(self.gate), lock(self.values["TESTER_LOCK_FILE"]):
            if self.busy_state():
                raise Busy("control task active; update deferred")
            backup = self.backup(previous, target)
            self.snapshot("prepared", previous, target, backup=str(backup))
            stopped = False
            try:
                self.stop(bootstrap=bootstrap)
                stopped = True
                self.snapshot("stopped", previous, target, backup=str(backup))
                self.hook("copy-config", previous, target)
                self.hook("post-copy", previous, target)
                self.switch(target)
                self.snapshot("switched", previous, target, backup=str(backup))
                command(["systemctl", "start", self.unit], timeout=65)
                self.health(target)
                self.hook("finalize", previous, target)
                provenance = json.loads((target / "host-adaptations.json").read_text())
                atomic_json(self.installed, {"release": str(target), "previous": str(previous), **provenance})
                self.snapshot("completed", previous, target, backup=str(backup))
            except Busy:
                self.snapshot("deferred", previous, target)
                raise
            except BaseException:
                if stopped:
                    self.rollback(previous, target)
                else:
                    # Even a stop command failure may have partially stopped
                    # the service; restore it before accepting another check.
                    command(["systemctl", "start", self.unit], timeout=65)
                    self.health(previous)
                    self.snapshot("rolled_back", previous, target)
                raise
        return "updated"

    def run(self, adopt=False, check=False):
        with lock(self.config["update_lock"]):
            self.recover()
            current = self.current()
            current_version = json.loads((current / "release.json").read_text())["version"]
            registry = Registry(self.config["credential_file"], self.state / "downloads")
            name = select_manifest(registry.manifests(), current_version, adopt)
            if not name:
                return {"status": "up_to_date", "version": current_version, "release": str(current)}
            manifest, artifact = validate_manifest(name, registry.small(name))
            sidecar = registry.small(artifact["package"] + ".sha256sum", 4096).decode().split()
            if len(sidecar) != 2 or sidecar[0] != artifact["sha256"] or sidecar[1].lstrip("*") != artifact["package"]:
                raise Refused("artifact sidecar mismatch")
            if check:
                return {"status": "candidate", "manifest": name, "sha256": artifact["sha256"]}
            archive = registry.archive(artifact)
            target = self.stage(archive, manifest, artifact)
            status = self.activate(target, bootstrap=adopt)
            return {"status": status, "version": artifact["version"], "source_commit": manifest["source_commit"], "release": str(target)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--adopt-current-version", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    config = json.loads(private_file(args.config).read_text())
    updater = Updater(config)
    def terminate(signum, frame):
        raise InterruptedError("update interrupted")
    signal.signal(signal.SIGTERM, terminate)
    try:
        result = updater.run(args.adopt_current_version, args.check)
        code = 0
    except Busy as error:
        result, code = {"status": "deferred", "reason": str(error)}, 0
    except Refused as error:
        result, code = {"status": "failed", "reason": str(error)}, 1
    except Exception as error:
        result, code = {"status": "failed", "reason": "updater operation failed", "error_type": type(error).__name__}, 1
    result["checked_at"] = datetime.now(timezone.utc).isoformat()
    atomic_json(updater.state / "last-check.json", result)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())

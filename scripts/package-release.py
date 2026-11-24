#!/usr/bin/env python3
"""Build a Linux Tester service from committed sources, without publishing."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SEMVER = r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
DEFAULT_BROWSER_CACHE = Path.home() / ".cache" / "yeying" / "tester" / "playwright"
DEFAULT_NPM_REGISTRY = "https://registry.npmmirror.com"
DEFAULT_PLAYWRIGHT_DOWNLOAD_HOST = "https://npmmirror.com/mirrors/playwright"
FILES = ("package.json", ".nvmrc", ".env.template",
         "LICENSE", "README.md", "playwright.config.ts", "playwright.safe.config.ts", "tsconfig.json")
DIRS = ("products", "shared", "scripts", "config", "docs")

def run(args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)

def install_dependencies(checkout, env):
    npm_version = subprocess.check_output(["npm", "--version"], text=True).strip()
    # Lockfiles are intentionally not committed for this project. Avoid
    # creating an untracked package-lock in the isolated release checkout.
    run(["npm", "install", "--package-lock=false", "--include=dev"], cwd=checkout, env=env)
    return npm_version


def build_web(checkout, env):
    web = checkout / "web"
    run(["npm", "ci", "--no-audit", "--no-fund"], cwd=web, env=env)
    run(["npm", "run", "build"], cwd=web, env=env)
    run(["npm", "prune", "--omit=dev", "--no-audit", "--no-fund"], cwd=web, env=env)


def browser_cache_path():
    """Return the persistent Playwright cache used by package builds.

    The cache is outside the temporary checkout and therefore survives each
    compile_packages run.  TESTER_BUILD_BROWSER_CACHE remains an explicit
    override for CI or an isolated build worker.
    """
    configured = os.environ.get("TESTER_BUILD_BROWSER_CACHE")
    return Path(configured).expanduser().absolute() if configured else DEFAULT_BROWSER_CACHE.absolute()


def required_browser_directories(checkout):
    """List browser directories needed by the safe Tester runtime.

    The safe configs select the full Chromium channel, so the separate
    headless-shell download is unnecessary and would add about 126 MiB.
    """
    manifest = checkout / "node_modules" / "playwright-core" / "browsers.json"
    browsers = json.loads(manifest.read_text())["browsers"]
    required = {"chromium", "ffmpeg"}
    return [f"{entry['name']}-{entry['revision']}"
            for entry in browsers
            if entry.get("installByDefault") and entry["name"] in required]


def copy_browser_bundle(cache, destination, directory_names):
    """Copy only this Playwright version's browsers into the release."""
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    for name in directory_names:
        source = cache / name
        if not source.is_dir():
            raise FileNotFoundError(f"Playwright browser cache is missing {name}")
        shutil.copytree(source, destination / name, symlinks=True)


def browser_install_command():
    return ["node", "node_modules/@playwright/test/cli.js", "install", "chromium", "--no-shell"]


def make_release_links_portable(staged):
    """Replace internal dependency symlinks with self-contained release files."""
    staged = staged.resolve()
    for path in list(staged.rglob("*")):
        if not path.is_symlink():
            continue
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(staged) or not resolved.is_file():
            raise ValueError("non-portable dependency link")
        path.unlink()
        shutil.copy2(resolved, path)
        if ".bin" in path.relative_to(staged).parts:
            path.chmod(path.stat().st_mode | 0o111)

def git(*args):
    return subprocess.check_output(["git", "-C", str(ROOT), *args], text=True).strip()

def sha(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1048576), b""):
            h.update(chunk)
    return h.hexdigest()


def release_manifest(source, service_version, web_version, service_package,
                     service_root, web_package, web_root):
    return {
        "schema_version": 1,
        "repository": "tester",
        "source_commit": source,
        "artifacts": [
            {"unit_id": "tester-service", "type": "service", "version": service_version,
             "package": service_package.name, "sha256": sha(service_package),
             "package_root": service_root,
             "service_target": {"module": "tester", "path": "/opt/deploy/tester"}},
            {"unit_id": "tester-web", "type": "static", "version": web_version,
             "package": web_package.name, "sha256": sha(web_package),
             "package_root": web_root,
             "static_targets": [{"site_id": "tester-web", "path": "/opt/deploy/static/tester-web",
                                 "health_check_path": "/index.html"}]},
        ],
    }

def publishable(relative):
    parts = relative.parts
    return not any(p in {".git", "__pycache__", ".cache", ".links"} or p.startswith(".project") for p in parts) and not (
        relative.name.endswith((".pyc", ".log")) or
        (relative.name.startswith(".env") and relative.name != ".env.template")
    )

def build(args):
    if not re.fullmatch(SEMVER, args.version):
        raise ValueError("version must be major.minor.patch")
    ref = git("rev-parse", "--verify", args.ref + "^{commit}")
    source = git("rev-parse", "--verify", args.source_commit + "^{commit}")
    if not re.fullmatch("[0-9a-f]{40}", args.source_commit) or args.source_commit != source:
        raise ValueError("source-commit must be a full commit SHA")
    run(["git", "-C", str(ROOT), "merge-base", "--is-ancestor", source, ref],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if sys.platform != "linux" or os.uname().machine not in ("x86_64", "amd64"):
        raise ValueError("this service package currently targets Linux x86_64")
    output = Path(args.output_dir).absolute()
    if output.exists() and (output.is_symlink() or not output.is_dir() or any(output.iterdir())):
        raise ValueError("output-dir must be an empty directory")
    # Read per-unit overrides from the selected release, never from dirty working files.
    override = subprocess.run(["git", "-C", str(ROOT), "show", ref + ":scripts/artifact-versions.conf"],
                              capture_output=True, text=True)
    versions = {"tester-service": args.version, "tester-web": args.version}
    if override.returncode == 0:
        for line in override.stdout.splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                unit, version = line.split()
                if unit not in ("tester-service", "tester-web") or not re.fullmatch(SEMVER, version):
                    raise ValueError("invalid Tester artifact version override")
                versions[unit] = version
    unit_version = versions["tester-service"]
    web_version = versions["tester-web"]
    service_root = f"tester-service-v{unit_version}-{source[:7]}"
    web_root = f"tester-web-v{web_version}-{source[:7]}"
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="tester-package-") as temporary:
        temp = Path(temporary)
        checkout = temp / "source"
        checkout.mkdir()
        # git archive reads exactly the selected commit; no fetch, checkout, tags or pushes.
        archive = temp / "source.tar"
        run(["git", "-C", str(ROOT), "archive", "--format=tar", "--output", str(archive), ref])
        with tarfile.open(archive) as tar:
            tar.extractall(checkout, filter="data") if hasattr(tarfile, "data_filter") else tar.extractall(checkout)
        manifest_api = json.loads((checkout / "scripts/package-api.json").read_text())
        if manifest_api["api_version"] != 1:
            raise ValueError("unsupported package API")
        node_version = subprocess.check_output(["node", "-p", "process.versions.node"], text=True).strip()
        major, minor, *_ = map(int, node_version.split("."))
        if (major, minor) < (24, 18):
            raise ValueError("Node.js >=24.18 is required")
        env = dict(os.environ, PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD="1")
        # Keep package builds usable on the mainland network while allowing
        # CI or an enterprise mirror to override each source explicitly.
        env.setdefault("NPM_CONFIG_REGISTRY", DEFAULT_NPM_REGISTRY)
        npm_version = install_dependencies(checkout, env)
        build_web(checkout, env)
        browsers = checkout / "browsers"
        cache = browser_cache_path()
        env["PLAYWRIGHT_BROWSERS_PATH"] = str(cache)
        env.setdefault("PLAYWRIGHT_DOWNLOAD_HOST", DEFAULT_PLAYWRIGHT_DOWNLOAD_HOST)
        env.pop("PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD", None)
        run(browser_install_command(), cwd=checkout, env=env)
        copy_browser_bundle(cache, browsers, required_browser_directories(checkout))
        staged = temp / service_root
        staged.mkdir()
        for name in FILES:
            shutil.copy2(checkout / name, staged / name)
        for name in (*DIRS, "node_modules", "browsers"):
            shutil.copytree(checkout / name, staged / name, symlinks=True)
        (staged / "release.json").write_text(json.dumps({
            "unit_id": "tester-service", "version": unit_version, "source_commit": source,
            "release_commit": ref, "platform": "linux-x86_64",
            "node": node_version, "npm": npm_version,
        }, indent=2) + "\n")
        # Resolve links only when they stay in this release, preventing external build paths.
        make_release_links_portable(staged)
        web_staged = temp / web_root
        shutil.copytree(checkout / "web/dist", web_staged)
        (web_staged / "release.json").write_text(json.dumps({
            "unit_id": "tester-web", "version": web_version, "source_commit": source,
        }, indent=2) + "\n")

        def archive_directory(directory, root_name):
            archive_path = temp / (root_name + ".tar.gz")
            def filtered(info):
                relative = Path(info.name).relative_to(root_name)
                if not publishable(relative):
                    return None
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                return info
            with tarfile.open(archive_path, "w:gz", dereference=True) as tar:
                tar.add(directory, arcname=root_name, filter=filtered)
            return archive_path

        archive_path = archive_directory(staged, service_root)
        web_archive_path = archive_directory(web_staged, web_root)
        manifest = release_manifest(source, unit_version, web_version, archive_path,
                                   service_root, web_archive_path, web_root)
        manifest_path = temp / "release-manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
        # Never replace existing artifact bytes; reserve output names before writing.
        for path in (archive_path, web_archive_path, manifest_path):
            with (output / path.name).open("xb") as destination, path.open("rb") as source_file:
                shutil.copyfileobj(source_file, destination)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    try:
        build(args)
    except (ValueError, OSError, KeyError, subprocess.SubprocessError, tarfile.TarError) as error:
        print(f"Tester packaging failed: {error}", file=sys.stderr)
        return 1
    return 0

if __name__ == "__main__":
    raise SystemExit(main())

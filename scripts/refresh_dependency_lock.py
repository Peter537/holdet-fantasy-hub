"""Regenerate the audited Windows/Python 3.14 dependency lock."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import tomllib
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = PROJECT_ROOT / "pyproject.toml"
LOCK_PATH = PROJECT_ROOT / "pylock.toml"
PYPI_INDEX = "https://pypi.org/simple"
PYPI_JSON = "https://pypi.org/pypi/{name}/{version}/json"
PYPI_ARTIFACT_HOST = "files.pythonhosted.org"
OSV_QUERY_BATCH = "https://api.osv.dev/v1/querybatch"
EXPECTED_PYTHON = (3, 14)
MINIMUM_PIP = (26, 0, 1)
REQUIREMENT_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
USER_AGENT = "holdet-fantasy-hub-dependency-lock/1"


class LockError(RuntimeError):
    """Raised when dependency resolution or verification cannot pass safely."""


def canonical_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).casefold()


def requirement_name(requirement: str) -> str:
    match = REQUIREMENT_NAME.match(requirement)
    if match is None or "@" in requirement or "://" in requirement or "git+" in requirement:
        raise LockError(f"Only public registry requirements are supported: {requirement!r}")
    return canonical_name(match.group(1))


def load_requirements() -> tuple[str, ...]:
    manifest = tomllib.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    requirements = [*manifest["build-system"]["requires"], *manifest["project"]["dependencies"]]
    for group in manifest["project"].get("optional-dependencies", {}).values():
        requirements.extend(group)
    for requirement in requirements:
        requirement_name(requirement)
    return tuple(dict.fromkeys(requirements))


def ensure_supported_runtime(*, require_lock_command: bool = True) -> None:
    if sys.platform != "win32" or sys.version_info[:2] != EXPECTED_PYTHON:
        raise LockError("The lock must be generated on Windows with CPython 3.14.")
    if not require_lock_command:
        return
    completed = subprocess.run(
        [sys.executable, "-m", "pip", "--version"],
        check=True,
        capture_output=True,
        text=True,
    )
    match = re.search(r"\bpip\s+(\d+)\.(\d+)\.(\d+)", completed.stdout)
    if match is None or tuple(int(part) for part in match.groups()) < MINIMUM_PIP:
        raise LockError("pip 26.0.1 or newer is required for PEP 751 lock support.")


def generate_lock(requirements: tuple[str, ...], destination: Path) -> None:
    command = [
        sys.executable,
        "-m",
        "pip",
        "lock",
        "--isolated",
        "--no-input",
        "--no-cache-dir",
        "--disable-pip-version-check",
        "--only-binary=:all:",
        "--only-final=:all:",
        "--index-url",
        PYPI_INDEX,
        "--output",
        str(destination),
        "--quiet",
        *requirements,
    ]
    try:
        subprocess.run(command, cwd=PROJECT_ROOT, check=True)
    except subprocess.CalledProcessError as exc:
        raise LockError(f"pip lock failed with exit code {exc.returncode}.") from exc


def load_and_validate_lock(
    path: Path,
    requirements: tuple[str, ...],
) -> tuple[dict[str, Any], tuple[dict[str, str], ...]]:
    try:
        lock = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise LockError(f"The generated lock is not readable TOML: {exc}") from exc
    if lock.get("lock-version") != "1.0" or lock.get("created-by") != "pip":
        raise LockError("The generated lock is not the expected pip PEP 751 format.")

    packages = lock.get("packages")
    if not isinstance(packages, list) or not packages:
        raise LockError("The generated lock contains no packages.")
    direct_names = {requirement_name(requirement) for requirement in requirements}
    locked_names: set[str] = set()
    artifacts: list[dict[str, str]] = []
    forbidden_sources = {"sdist", "archive", "vcs", "directory"}
    for package in packages:
        if not isinstance(package, dict):
            raise LockError("The generated lock contains an invalid package entry.")
        name = canonical_name(str(package.get("name", "")))
        version = str(package.get("version", ""))
        if not name or not version or name in locked_names:
            raise LockError(f"Invalid or duplicate locked package: {name!r} {version!r}")
        locked_names.add(name)
        if forbidden_sources.intersection(package):
            raise LockError(f"{name} resolved from a forbidden non-wheel source.")
        wheels = package.get("wheels")
        if not isinstance(wheels, list) or len(wheels) != 1:
            raise LockError(f"{name} must resolve to exactly one platform wheel.")
        wheel = wheels[0]
        if not isinstance(wheel, dict):
            raise LockError(f"{name} has invalid wheel metadata.")
        filename = str(wheel.get("name", ""))
        url = str(wheel.get("url", ""))
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != PYPI_ARTIFACT_HOST
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise LockError(f"{name} resolved from an untrusted artifact URL: {url}")
        hashes = wheel.get("hashes")
        digest = str(hashes.get("sha256", "")) if isinstance(hashes, dict) else ""
        if not SHA256.fullmatch(digest):
            raise LockError(f"{name} does not have a valid SHA-256 wheel hash.")
        artifacts.append(
            {"name": name, "version": version, "filename": filename, "url": url, "sha256": digest}
        )
    missing = sorted(direct_names - locked_names)
    if missing:
        raise LockError("The lock omits direct requirements: " + ", ".join(missing))
    return lock, tuple(artifacts)


def request_json(url: str, *, body: bytes | None = None) -> Any:
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = Request(url, data=body, headers=headers, method="POST" if body is not None else "GET")
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with urlopen(request, timeout=30) as response:  # noqa: S310 - URLs are fixed above.
                return json.load(response)
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(2**attempt)
    raise LockError(f"Authoritative dependency evidence is unavailable from {url}: {last_error}")


def verify_pypi_artifacts(artifacts: tuple[dict[str, str], ...]) -> int:
    provenance_gaps = 0
    for artifact in artifacts:
        url = PYPI_JSON.format(name=quote(artifact["name"]), version=quote(artifact["version"]))
        metadata = request_json(url)
        if canonical_name(str(metadata.get("info", {}).get("name", ""))) != artifact["name"]:
            raise LockError(f"PyPI identity mismatch for {artifact['name']}.")
        files = [item for item in metadata.get("urls", []) if item.get("filename") == artifact["filename"]]
        if len(files) != 1:
            raise LockError(f"PyPI does not expose the selected wheel for {artifact['name']}.")
        selected = files[0]
        registry_digest = str(selected.get("digests", {}).get("sha256", ""))
        if (
            selected.get("packagetype") != "bdist_wheel"
            or selected.get("yanked") is True
            or selected.get("url") != artifact["url"]
            or registry_digest != artifact["sha256"]
        ):
            raise LockError(f"PyPI artifact evidence does not match the lock for {artifact['name']}.")
        if not selected.get("has_sig") and not selected.get("provenance"):
            provenance_gaps += 1
    return provenance_gaps


def verify_osv(artifacts: tuple[dict[str, str], ...]) -> None:
    payload = {
        "queries": [
            {
                "package": {"ecosystem": "PyPI", "name": artifact["name"]},
                "version": artifact["version"],
            }
            for artifact in artifacts
        ]
    }
    result = request_json(OSV_QUERY_BATCH, body=json.dumps(payload, separators=(",", ":")).encode())
    records = result.get("results") if isinstance(result, dict) else None
    if not isinstance(records, list) or len(records) != len(artifacts):
        raise LockError("OSV returned an incomplete result set.")
    findings: list[str] = []
    for artifact, record in zip(artifacts, records, strict=True):
        identifiers = sorted(
            str(item["id"])
            for item in record.get("vulns", [])
            if isinstance(item, dict) and item.get("id")
        )
        if identifiers:
            findings.append(f"{artifact['name']}=={artifact['version']}: {', '.join(identifiers)}")
    if findings:
        raise LockError("OSV found advisories in the candidate lock:\n" + "\n".join(findings))


def replace_lock_atomically(content: bytes) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=".pylock.",
            suffix=".tmp",
            dir=PROJECT_ROOT,
            delete=False,
        ) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        os.replace(temporary, LOCK_PATH)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def run(check: bool) -> int:
    ensure_supported_runtime(require_lock_command=True)
    requirements = load_requirements()
    with tempfile.TemporaryDirectory(prefix="holdet-dependency-lock-") as directory:
        candidate = Path(directory) / "pylock.toml"
        generate_lock(requirements, candidate)
        _, artifacts = load_and_validate_lock(candidate, requirements)
        provenance_gaps = verify_pypi_artifacts(artifacts)
        verify_osv(artifacts)
        content = candidate.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    if check:
        if not LOCK_PATH.is_file() or LOCK_PATH.read_bytes() != content:
            raise LockError("pylock.toml is stale; run this script without --check to refresh it.")
        action = "verified"
    else:
        replace_lock_atomically(content)
        action = "updated"
    print(
        f"{action} {LOCK_PATH.name}: {len(artifacts)} packages, sha256={digest}, "
        f"OSV clean, {provenance_gaps} artifacts without registry signature/provenance"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Verify that pylock.toml is current.")
    arguments = parser.parse_args(argv)
    try:
        return run(arguments.check)
    except (LockError, OSError, subprocess.SubprocessError) as exc:
        print(f"dependency lock failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

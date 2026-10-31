from __future__ import annotations

from pathlib import Path
import re
import tomllib
from urllib.parse import urlsplit

from scripts import refresh_dependency_lock


PROJECT_ROOT = Path(__file__).parents[1]
REQUIREMENT_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def canonical_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).casefold()


def direct_requirements(manifest: dict[str, object]) -> dict[str, str]:
    build = manifest["build-system"]  # type: ignore[index]
    project = manifest["project"]  # type: ignore[index]
    requirements = [*build["requires"], *project["dependencies"]]  # type: ignore[index]
    for group in project["optional-dependencies"].values():  # type: ignore[index,union-attr]
        requirements.extend(group)
    parsed: dict[str, str] = {}
    for requirement in requirements:
        assert "@" not in requirement and "://" not in requirement and "git+" not in requirement
        match = REQUIREMENT_NAME.match(requirement)
        assert match is not None
        parsed[canonical_name(match.group(1))] = requirement
    return parsed


def test_manifest_uses_audited_dependency_ranges() -> None:
    manifest = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    requirements = direct_requirements(manifest)
    assert requirements == {
        "pip": "pip>=26.2.2,<27",
        "setuptools": "setuptools>=84.1,<85",
        "platformdirs": "platformdirs>=4.11.1,<5",
        "xlsxwriter": "xlsxwriter>=3.2.9,<4",
        "streamlit": "streamlit>=1.66.1,<2",
        "starlette": "starlette>=1.3.1,<1.4",
        "pandas": "pandas>=3.0.5,<4",
        "altair": "altair>=6.2.2,<7",
        "pyarrow": "pyarrow>=24,<25",
        "pytest": "pytest>=9.1.1,<10",
        "httpx2": "httpx2>=2.12,<3",
        "playwright": "playwright>=1.62,<2",
        "pytest-playwright": "pytest-playwright>=0.8,<1",
        "pillow": "pillow>=13,<14",
    }


def test_lock_covers_manifest_with_trusted_hashed_wheels() -> None:
    manifest = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    lock = tomllib.loads((PROJECT_ROOT / "pylock.toml").read_text(encoding="utf-8"))
    assert lock["lock-version"] == "1.0"
    assert lock["created-by"] == "pip"

    packages = lock["packages"]
    locked_names: set[str] = set()
    for package in packages:
        name = canonical_name(package["name"])
        assert name not in locked_names
        locked_names.add(name)
        assert package["version"]
        assert {"sdist", "archive", "vcs", "directory"}.isdisjoint(package)
        assert len(package["wheels"]) == 1
        wheel = package["wheels"][0]
        parsed = urlsplit(wheel["url"])
        assert parsed.scheme == "https"
        assert parsed.hostname == "files.pythonhosted.org"
        assert parsed.username is None and parsed.password is None
        assert wheel["name"].endswith(".whl")
        assert SHA256.fullmatch(wheel["hashes"]["sha256"])

    assert set(direct_requirements(manifest)) <= locked_names
    assert {"pandas", "altair", "httpx2"} <= locked_names
    assert {"httpx", "httpcore"}.isdisjoint(locked_names)


def test_lock_check_preserves_locked_versions_and_does_not_rewrite(monkeypatch, tmp_path) -> None:
    before = refresh_dependency_lock.LOCK_PATH.read_bytes()
    lock = tomllib.loads(before.decode("utf-8"))
    expected = {f"{item['name']}=={item['version']}" for item in lock["packages"]}
    evidence = []

    def resolve(_requirements, destination, *, constraints=None):
        assert constraints is not None
        assert set(constraints.read_text(encoding="utf-8").splitlines()) == expected
        destination.write_bytes(before)

    def reject_write(_content):
        raise AssertionError("check rewrote lock")

    monkeypatch.setattr(refresh_dependency_lock, "ensure_supported_runtime", lambda **_kwargs: None)
    monkeypatch.setattr(refresh_dependency_lock.tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(refresh_dependency_lock, "generate_lock", resolve)
    monkeypatch.setattr(
        refresh_dependency_lock, "verify_pypi_artifacts",
        lambda artifacts: evidence.append(("registry", len(artifacts))) or 0,
    )
    monkeypatch.setattr(
        refresh_dependency_lock, "verify_osv",
        lambda artifacts: evidence.append(("advisories", len(artifacts))),
    )
    monkeypatch.setattr(
        refresh_dependency_lock, "replace_lock_atomically",
        reject_write,
    )
    assert refresh_dependency_lock.run(check=True) == 0
    assert evidence == [("registry", len(expected)), ("advisories", len(expected))]
    assert refresh_dependency_lock.LOCK_PATH.read_bytes() == before

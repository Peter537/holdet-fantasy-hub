"""Install the exact hashed wheels recorded in pylock.toml."""

from __future__ import annotations

import subprocess
import sys
import tempfile

from refresh_dependency_lock import (
    LOCK_PATH,
    LockError,
    ensure_supported_runtime,
    load_and_validate_lock,
    load_requirements,
)


def render_hashed_requirements(artifacts: tuple[dict[str, str], ...]) -> str:
    return "".join(
        f"{artifact['name']} @ {artifact['url']} --hash=sha256:{artifact['sha256']}\n"
        for artifact in artifacts
    )


def run() -> int:
    ensure_supported_runtime(require_lock_command=False)
    requirements = load_requirements()
    _, artifacts = load_and_validate_lock(LOCK_PATH, requirements)
    with tempfile.TemporaryDirectory(prefix="holdet-locked-install-") as directory:
        requirements_path = f"{directory}\\requirements.txt"
        with open(requirements_path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(render_hashed_requirements(artifacts))
        command = [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--isolated",
            "--no-input",
            "--no-cache-dir",
            "--disable-pip-version-check",
            "--only-binary=:all:",
            "--require-hashes",
            "--no-deps",
            "--requirement",
            requirements_path,
        ]
        try:
            subprocess.run(command, check=True)
        except subprocess.CalledProcessError as exc:
            raise LockError(f"locked dependency installation failed with exit code {exc.returncode}.") from exc
    print(f"installed {len(artifacts)} exact hashed wheels from {LOCK_PATH.name}")
    return 0


def main() -> int:
    try:
        return run()
    except (LockError, OSError, subprocess.SubprocessError) as exc:
        print(f"locked dependency installation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

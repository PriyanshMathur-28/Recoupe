"""Report Git repository status without failing when Git metadata is absent."""
from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main() -> int:
    try:
        result = subprocess.run(
            ["git", "-C", str(ROOT), "status", "--short"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        print(f"Git status unavailable: {exc}")
        return 0
    if result.returncode != 0:
        message = (result.stderr or result.stdout).strip()
        if "not a git repository" in message.lower():
            print("Git status: this project directory is not a Git repository.")
            return 0
        print(f"Git status failed: {message}")
        return result.returncode
    print("Git status: clean" if not result.stdout.strip() else result.stdout.rstrip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

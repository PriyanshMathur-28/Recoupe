"""Safe project inspection commands for Windows and other shells."""
from __future__ import annotations

import ast
from importlib.util import find_spec
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REQUIRED_MODULES = {
    "flask": "flask",
    "apscheduler": "apscheduler",
    "google-api-python-client": "googleapiclient",
    "google-auth": "google.auth",
    "google-auth-oauthlib": "google_auth_oauthlib",
    "razorpay": "razorpay",
    "python-dotenv": "dotenv",
    "requests": "requests",
    "pandas": "pandas",
    "pytest": "pytest",
}


def imported_top_level_modules() -> set[str]:
    names: set[str] = set()
    paths = [ROOT / name for name in ("main.py", "dashboard.py", "batch_runner.py", "oauth_flow.py", "validate_csv.py")]
    paths.extend((ROOT / "modules").glob("*.py"))
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
    return names


def main() -> int:
    missing = [package for package, module in REQUIRED_MODULES.items() if find_spec(module) is None]
    print("Project:", ROOT)
    print("Imported top-level modules:", ", ".join(sorted(imported_top_level_modules())))
    if missing:
        print("Missing required packages:", ", ".join(missing))
        return 1
    print("All required Python packages are importable.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

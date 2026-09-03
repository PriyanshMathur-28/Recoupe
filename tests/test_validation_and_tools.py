"""Regression tests for data validation and safe project diagnostics."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from validate_csv import ROOT, SPECS, validate_file


def test_production_csv_fixtures_are_valid():
    errors = [
        f"{relative}: {error}"
        for relative, spec in SPECS.items()
        for error in validate_file(ROOT / relative, spec)
    ]
    assert errors == []


def test_validate_csv_cli_returns_success_for_valid_fixtures():
    result = subprocess.run(
        [sys.executable, "validate_csv.py"],
        cwd=Path(__file__).parents[1],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "No validation errors" in result.stdout


def test_project_inspection_cli_is_shell_safe():
    result = subprocess.run(
        [sys.executable, "inspect_project.py"],
        cwd=Path(__file__).parents[1],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "All required Python packages are importable." in result.stdout


def test_repository_check_handles_missing_git_metadata():
    result = subprocess.run(
        [sys.executable, "repository_check.py"],
        cwd=Path(__file__).parents[1],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "not a Git repository" in result.stdout

"""Smoke tests for the sbctl control script.

These don't actually start uvicorn — too much state to clean up safely
in a test run. They just verify the script ships, is executable,
passes ``bash -n`` syntax check, and is documented.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SBCTL = PROJECT_ROOT / "scripts" / "sbctl"
INSTALLER = PROJECT_ROOT / "scripts" / "install-sbctl.sh"
SETUP_LINUX = PROJECT_ROOT / "docs" / "SETUP_LINUX.md"
GITIGNORE = PROJECT_ROOT / ".gitignore"
RUNTIME_DIR = PROJECT_ROOT / "runtime"


def test_sbctl_script_exists():
    assert SBCTL.exists(), f"missing: {SBCTL}"
    assert SBCTL.is_file()


def test_sbctl_script_is_executable():
    mode = SBCTL.stat().st_mode
    assert mode & stat.S_IXUSR, "scripts/sbctl is not user-executable"


def test_sbctl_passes_bash_syntax_check():
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash not available")
    result = subprocess.run(
        [bash, "-n", str(SBCTL)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


def test_sbctl_help_lists_all_commands():
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash not available")
    result = subprocess.run(
        [bash, str(SBCTL), "--help"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    out = result.stdout
    for command in (
        "start", "stop", "restart", "status", "logs", "health", "audit",
    ):
        assert command in out, f"sbctl --help missing {command!r}"


def test_install_script_exists_and_is_executable():
    assert INSTALLER.exists()
    mode = INSTALLER.stat().st_mode
    assert mode & stat.S_IXUSR, "install-sbctl.sh is not user-executable"


def test_install_script_help_runs():
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash not available")
    result = subprocess.run(
        [bash, str(INSTALLER), "--help"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert "sbctl" in result.stdout


def test_runtime_directory_exists():
    assert RUNTIME_DIR.is_dir(), "runtime/ must exist for sbctl pid file"
    assert (RUNTIME_DIR / ".gitkeep").exists(), "runtime/.gitkeep missing"


def test_gitignore_lists_runtime_and_server_log():
    text = GITIGNORE.read_text()
    assert "runtime/*.pid" in text or "runtime/*" in text, (
        ".gitignore must ignore runtime/*.pid"
    )
    assert "logs/server.out" in text or "logs/*" in text, (
        ".gitignore must ignore logs/server.out"
    )


def test_docs_mention_sbctl_usage():
    text = SETUP_LINUX.read_text()
    for command in ("sbctl start", "sbctl stop", "sbctl restart",
                    "sbctl status", "sbctl logs", "sbctl health"):
        assert command in text, f"SETUP_LINUX.md missing {command!r}"
    assert "install-sbctl.sh" in text
    assert "127.0.0.1:8000" in text

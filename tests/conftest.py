"""Shared fixtures for the managed-agent test suite."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """An isolated HERMES_HOME for a materialized working copy."""
    path = tmp_path / "home"
    path.mkdir()
    return path


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """An isolated managed workspace."""
    path = tmp_path / "workspace"
    path.mkdir()
    return path

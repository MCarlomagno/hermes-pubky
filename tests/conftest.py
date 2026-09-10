"""Shared fixtures for the managed-agent test suite."""

from __future__ import annotations

import gc
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


def pytest_runtest_teardown(item, nextitem) -> None:
    """Collect garbage after every test.

    Warnings are errors here, and Python 3.13 warns about a database connection
    that is closed by the collector rather than by code. Collecting now blames
    the test that leaked it, on every Python version, instead of whichever test
    happened to be running when the collector got there.
    """
    del item, nextitem
    gc.collect()

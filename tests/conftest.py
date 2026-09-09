"""Shared fixtures: an isolated HERMES_HOME and the stores rooted in it."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_pubky.outbox import Outbox
from hermes_pubky.paths import Layout
from hermes_pubky.store import Store


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """An isolated HERMES_HOME."""
    h = tmp_path / "hermes"
    h.mkdir()
    return h


@pytest.fixture
def layout(home: Path) -> Layout:
    lay = Layout(home, "default")
    lay.ensure()
    return lay


@pytest.fixture
def store(layout: Layout) -> Store:
    return Store(layout)


@pytest.fixture
def outbox(layout: Layout) -> Outbox:
    return Outbox(layout.outbox)


@pytest.fixture
def fake_remote():
    from fakes import FakeRemote

    return FakeRemote()

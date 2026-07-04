from __future__ import annotations

import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).parent
sys.path.insert(0, str(TESTS_DIR))

from fixtures.make_decks import make_all  # noqa: E402


@pytest.fixture(scope="session")
def decks(tmp_path_factory) -> dict[str, Path]:
    """The five fixture decks, generated once per test session."""
    out = tmp_path_factory.mktemp("decks")
    return make_all(out)


@pytest.fixture()
def registry(tmp_path):
    from pptxsweeper.db import Registry
    reg = Registry(tmp_path / "registry.db")
    yield reg
    reg.close()

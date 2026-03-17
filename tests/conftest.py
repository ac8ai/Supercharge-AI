"""Shared test fixtures for SuperchargeAI test suite."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _isolate_metrics_db(request, tmp_path: Path):
    """Prevent _emit() from writing metrics.db to the repo root.

    Without this fixture, any code path that calls _emit() (directly or
    indirectly) resolves _db_path() -> _project_dir() -> CWD, which creates
    real metrics.db files under the repo root during test runs.

    By patching _db_path at the source (supercharge.metrics), all modules
    that import _emit from supercharge.metrics will use the tmp_path database,
    regardless of which module-level _emit reference is called.

    Tests that need the real _db_path (e.g., testing _db_path itself) can
    opt out with the @pytest.mark.no_isolate_metrics marker.
    """
    if request.node.get_closest_marker("no_isolate_metrics"):
        yield
        return

    with patch(
        "supercharge.metrics._db_path",
        return_value=tmp_path / "metrics.db",
    ):
        yield

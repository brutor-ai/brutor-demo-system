import os

import pytest

os.environ["GENERATE_ENABLED"] = "false"

from applications_mcp import server  # noqa: E402


@pytest.fixture
def store(tmp_path):
    """Fresh store in a temp dir, wired into the server module."""
    return server.configure(tmp_path)

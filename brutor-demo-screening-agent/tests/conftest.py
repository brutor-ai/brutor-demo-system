import pytest

from tests.fake_gateway import FakeGateway, settings_for_tests


@pytest.fixture
def settings(tmp_path):
    return settings_for_tests(tmp_path)


@pytest.fixture
def fake():
    return FakeGateway()

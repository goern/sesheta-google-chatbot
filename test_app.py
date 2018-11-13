import pytest
import app as service


@pytest.fixture
def api():
    return service.api


def test_metrics(api):
    r = api.requests.get("/metrics")
    assert r.text == 'sesheta_info{version="0.1.0-dev"} 1.0'

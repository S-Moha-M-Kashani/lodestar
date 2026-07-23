from fastapi.testclient import TestClient

from lodestar_brain.config import Settings
from lodestar_brain.server import create_app


def test_health():
    client = TestClient(create_app(Settings(llm_provider='fake', embedder='hash')))
    res = client.get('/health')
    assert res.status_code == 200
    assert res.json() == {'ok': True, 'service': 'lodestar-brain'}

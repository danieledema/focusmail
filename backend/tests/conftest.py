import os
import tempfile
os.environ['DATA_DIR'] = tempfile.mkdtemp(prefix='focusmail-tests-')
os.environ['DEMO_MODE'] = 'true'
import pytest
from fastapi.testclient import TestClient
from backend.main import app
from backend.database import Base,engine

@pytest.fixture
def client():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with TestClient(app) as client:
        yield client

"""pytest 根配置：把仓库根目录加入 sys.path，并提供临时库夹具。"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hydrology import create_app  # noqa: E402


@pytest.fixture()
def app(tmp_path):
    db_path = tmp_path / "test_hydrology.db"
    application = create_app({"TESTING": True, "DATABASE": str(db_path)})
    yield application


@pytest.fixture()
def client(app):
    return app.test_client()

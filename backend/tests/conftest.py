import atexit
import os
import shutil
import tempfile
from pathlib import Path

# Must be set before app.main is imported so no test can ever resolve to backend/lazydesk.db.
_TEST_DB_DIR = tempfile.mkdtemp(prefix="lazydesk-test-db-")
os.environ["LAZYDESK_DB"] = str(Path(_TEST_DB_DIR) / "session.sqlite")
atexit.register(shutil.rmtree, _TEST_DB_DIR, ignore_errors=True)

import pytest  # noqa: E402

from app import main  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_environment(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path_factory.mktemp("db") / "test.sqlite"
    monkeypatch.setattr(main, "DB_PATH", db_path)
    monkeypatch.delenv("OLLAMA_MODEL", raising=False)
    monkeypatch.setenv("OLLAMA_HOST", "http://127.0.0.1:9")
    yield
    assert main.DB_PATH != Path(__file__).resolve().parent.parent / "lazydesk.db"

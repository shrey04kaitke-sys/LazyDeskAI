import atexit
import os
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace

# Must be set before app.main is imported so no test can ever resolve to backend/lazydesk.db.
_TEST_DB_DIR = tempfile.mkdtemp(prefix="lazydesk-test-db-")
os.environ["LAZYDESK_DB"] = str(Path(_TEST_DB_DIR) / "session.sqlite")
atexit.register(shutil.rmtree, _TEST_DB_DIR, ignore_errors=True)

import pytest  # noqa: E402

from starlette.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402
from app.api_guard import TOKEN_HEADER  # noqa: E402

# EXPLICIT TEST CONFIGURATION for the Phase 3 API guard. Production defaults are untouched: the guard still demands a
# loopback peer, an allowlisted Host and the per-launch token. Here, and only here, the test peer name is trusted and every
# TestClient created by the suite gets a loopback Host plus the current process token, so the existing API tests keep
# exercising the real routes. test_api_guard.py builds clients through `raw_client_factory`, which skips these defaults.
main.GUARD.extra_peers = ("testclient",)
_ORIGINAL_INIT = TestClient.__init__


def _configured_init(self, app, *args, **kwargs):
    kwargs.setdefault("base_url", "http://127.0.0.1:8000")
    _ORIGINAL_INIT(self, app, *args, **kwargs)
    self.headers.setdefault(TOKEN_HEADER, main.GUARD.token)


TestClient.__init__ = _configured_init


@pytest.fixture
def raw_client_factory():
    """TestClients with NO default Host or token, for tests of the guard itself."""
    def make(app=main.app, **kwargs) -> TestClient:
        client = TestClient.__new__(TestClient)
        _ORIGINAL_INIT(client, app, **kwargs)
        return client
    return make


@pytest.fixture(autouse=True)
def fake_profile(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """A synthetic Users directory with one current profile, so tests never depend on the developer's real profile.

    pytest's own temp folder lives under the real AppData (a protected location), so the profile environment variables
    are pointed at a fake profile for the duration of each test. The protection rules themselves are unchanged and are
    exercised against this fake layout (and against the real system folders) in test_workspace_policy.py."""
    users = tmp_path_factory.mktemp("users")
    profile = users / "tester"
    for relative in ("AppData/Local", "AppData/Roaming", "Documents", "Downloads", "Desktop"):
        (profile / relative).mkdir(parents=True)
    (users / "Public").mkdir()
    for name, value in {
        "USERPROFILE": profile, "HOME": profile, "PUBLIC": users / "Public",
        "APPDATA": profile / "AppData" / "Roaming", "LOCALAPPDATA": profile / "AppData" / "Local",
    }.items():
        monkeypatch.setenv(name, str(value))
    return SimpleNamespace(users=users, profile=profile, public=users / "Public")


@pytest.fixture(autouse=True)
def _isolated_environment(fake_profile, tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path_factory.mktemp("db") / "test.sqlite"
    monkeypatch.setattr(main, "DB_PATH", db_path)
    monkeypatch.delenv("OLLAMA_MODEL", raising=False)
    monkeypatch.delenv("LAZYDESK_DEFAULT_MODEL", raising=False)
    monkeypatch.setenv("OLLAMA_HOST", "http://127.0.0.1:9")
    yield
    assert main.DB_PATH != Path(__file__).resolve().parent.parent / "lazydesk.db"

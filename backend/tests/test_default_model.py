"""The built-in default model (qwen2.5:3b) sits at the bottom of the precedence list: every explicit choice beats it,
it is only used if installed, and nothing is ever downloaded. Mocked server; no live Ollama."""
from pathlib import Path

import pytest

from app import main
from model_fakes import FakeOllama, answer, client, scan, use_model, workspace  # noqa: F401

DEFAULT = "qwen2.5:3b"
ROOT = Path(__file__).resolve().parents[2]


def save_setting(value):
    connection = main.db()
    connection.execute("INSERT INTO settings(key, value) VALUES ('ollama_model', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (value,))
    connection.commit()
    connection.close()


def install(monkeypatch, *models):
    fake = FakeOllama(lambda _b: answer("documents", None, "A document by name."), models=models)
    monkeypatch.setattr(main.urllib.request, "urlopen", fake)
    return fake


def write(root: Path, name: str = "mystery.xyz") -> Path:
    path = root / name
    path.write_bytes(b"synthetic")
    return path


def test_the_default_is_qwen_and_documented_in_one_place():
    assert main.DEFAULT_OLLAMA_MODEL == DEFAULT
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert DEFAULT in readme and "LAZYDESK_DEFAULT_MODEL" in readme and "small synthetic" in readme


# ---- precedence table -----------------------------------------------------------------------------------------

UNSET = object()


@pytest.mark.parametrize("saved,environment,default_env,expected", [
    (UNSET, UNSET, UNSET, (DEFAULT, "default")),
    (UNSET, UNSET, "", (None, "none")),                     # the built-in default can be switched off
    (UNSET, UNSET, "d1", ("d1", "default")),                # ...or replaced
    (UNSET, "", UNSET, (None, "none")),                     # OLLAMA_MODEL= (empty) means "no model", as it always did
    (UNSET, "e1", UNSET, ("e1", "environment")),
    (UNSET, "e1", "d1", ("e1", "environment")),             # an explicit environment choice beats the default
    (None, UNSET, UNSET, (None, "none")),                   # "Deterministic fallback" picked in Settings beats the default
    (None, "e1", UNSET, ("e1", "environment")),             # unchanged historical precedence: the environment beat a NULL row
    ("s1", UNSET, UNSET, ("s1", "setting")),
    ("s1", "e1", "d1", ("s1", "setting")),                  # a saved choice beats everything
])
def test_model_choice_precedence(monkeypatch, saved, environment, default_env, expected):
    if saved is not UNSET:
        save_setting(saved)
    if environment is not UNSET:
        monkeypatch.setenv("OLLAMA_MODEL", environment)
    if default_env is not UNSET:
        monkeypatch.setenv("LAZYDESK_DEFAULT_MODEL", default_env)
    assert main.ollama_model_choice() == expected
    assert main.ollama_settings()[1] == expected[0]


# ---- behaviour ----------------------------------------------------------------------------------------------------

def test_with_no_configuration_the_installed_default_is_used(workspace, client, monkeypatch):
    write(workspace)
    fake = install(monkeypatch, DEFAULT, "other:1b")
    result = scan(client, workspace)
    assert result["processing"]["model_requests"] == 1 and result["items"][0]["classification_source"] == "ollama"
    assert fake.generate_calls[0]["model"] == DEFAULT


def test_when_the_default_is_not_installed_scans_stay_deterministic_and_nothing_is_requested(workspace, client, monkeypatch):
    write(workspace)
    fake = install(monkeypatch, "other:1b")
    result = scan(client, workspace)
    assert result["processing"]["model_requests"] == 0 and fake.generate_calls == [] and fake.warmup_calls == []
    assert result["items"][0]["classification_source"] == "local-rule" and result["items"][0]["category"] == "other"
    health = client.get("/api/status").json()["ollama"]
    assert health["selected_model"] == DEFAULT and health["model_source"] == "default" and health["selected_model_installed"] is False


def test_the_default_never_triggers_a_download_or_other_endpoints(workspace, client, monkeypatch):
    write(workspace)
    fake = install(monkeypatch, "other:1b")  # default NOT installed
    scan(client, workspace)
    fake.loaded = False
    scan(client, workspace)
    assert {endpoint for endpoint, _timeout in fake.timeouts} <= {"tags", "ps", "generate"}  # never /api/pull
    assert fake.generate_calls == []


def test_an_environment_choice_overrides_the_default(workspace, client, monkeypatch):
    write(workspace)
    monkeypatch.setenv("OLLAMA_MODEL", "m1")
    fake = install(monkeypatch, DEFAULT, "m1")
    scan(client, workspace)
    assert [call["model"] for call in fake.generate_calls] == ["m1"]


def test_an_empty_environment_value_disables_the_default_even_when_it_is_installed(workspace, client, monkeypatch):
    write(workspace)
    monkeypatch.setenv("OLLAMA_MODEL", "")
    fake = install(monkeypatch, DEFAULT)
    assert scan(client, workspace)["processing"]["model_requests"] == 0 and fake.generate_calls == []


def test_choosing_deterministic_in_settings_persists_and_beats_the_default(workspace, client, monkeypatch):
    write(workspace)
    fake = install(monkeypatch, DEFAULT)
    assert client.patch("/api/settings", json={"model": None}).status_code == 200
    settings = client.get("/api/settings").json()
    assert settings["model"] is None and settings["source"] == "none" and settings["default_model"] == DEFAULT
    assert scan(client, workspace)["processing"]["model_requests"] == 0 and fake.generate_calls == []
    assert client.get("/api/status").json()["ollama"]["model_source"] == "none"


def test_a_saved_model_beats_the_default_and_must_be_installed(workspace, client, monkeypatch):
    write(workspace)
    fake = install(monkeypatch, DEFAULT, "m1")
    assert client.patch("/api/settings", json={"model": "not-installed:7b"}).status_code == 400  # existing safeguard kept
    assert client.patch("/api/settings", json={"model": "m1"}).status_code == 200
    settings = client.get("/api/settings").json()
    assert settings["model"] == "m1" and settings["source"] == "setting"
    scan(client, workspace)
    assert [call["model"] for call in fake.generate_calls] == ["m1"]


def test_the_default_can_be_replaced_with_an_environment_variable(workspace, client, monkeypatch):
    write(workspace)
    monkeypatch.setenv("LAZYDESK_DEFAULT_MODEL", "d1")
    fake = install(monkeypatch, "d1", DEFAULT)
    scan(client, workspace)
    assert [call["model"] for call in fake.generate_calls] == ["d1"]
    assert client.get("/api/settings").json()["source"] == "default"


def test_health_and_settings_report_where_the_choice_came_from(client, monkeypatch):
    install(monkeypatch, DEFAULT)
    health = client.get("/api/status").json()["ollama"]
    assert (health["selected_model"], health["model_source"], health["selected_model_installed"], health["default_model"]) == (DEFAULT, "default", True, DEFAULT)
    monkeypatch.setenv("OLLAMA_MODEL", "e1")
    assert client.get("/api/status").json()["ollama"]["model_source"] == "environment"


def test_an_unreachable_server_with_the_default_configured_is_simply_deterministic(workspace, client):
    write(workspace)  # conftest points OLLAMA_HOST at an unreachable port and does not stub urlopen
    result = scan(client, workspace)
    assert result["processing"]["model_requests"] == 0 and result["items"][0]["classification_source"] == "local-rule"

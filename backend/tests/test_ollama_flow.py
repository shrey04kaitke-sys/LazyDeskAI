"""Model inference -> cache -> review plan, with the local model fully mocked (no real Ollama is contacted)."""
import json
import urllib.error
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main


class Response:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self):
        if isinstance(self.body, bytes):
            return self.body
        if isinstance(self.body, str):
            return self.body.encode()
        return json.dumps(self.body).encode()


class FakeOllama:
    """Stands in for urllib.request.urlopen. `handler(request_json)` returns the HTTP body or raises."""

    def __init__(self, handler, models=("m1",)):
        self.handler, self.models, self.generate_calls = handler, list(models), []

    def __call__(self, request, **_kwargs):
        if request.full_url.endswith("/api/tags"):
            return Response({"models": [{"name": name} for name in self.models]})
        body = json.loads(request.data)
        self.generate_calls.append(body)
        return Response(self.handler(body))


def answer(category="images", name="renamed.xyz", reason="Looks like a picture."):
    return {"response": json.dumps({"category": category, "proposed_name": name, "reason": reason})}


@pytest.fixture
def client() -> TestClient:
    return TestClient(main.app)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    root.mkdir()
    return root


def use_model(monkeypatch: pytest.MonkeyPatch, handler, models=("m1",), selected="m1") -> FakeOllama:
    fake = FakeOllama(handler, models)
    monkeypatch.setattr(main.urllib.request, "urlopen", fake)
    monkeypatch.setenv("OLLAMA_MODEL", selected)
    return fake


def scan(client: TestClient, root: Path, **extra) -> dict:
    response = client.post("/api/scan", json={"path": str(root), **extra})
    assert response.status_code == 200, response.text
    return response.json()


def plan(client: TestClient, root: Path, *files: Path) -> dict:
    response = client.post("/api/plans", json={"workspace_root": str(root), "files": [str(item) for item in files]})
    assert response.status_code == 200, response.text
    return response.json()


# ---- the model's valid answer reaches the review plan ------------------------------

def test_valid_model_answer_drives_category_name_and_source_in_the_plan(workspace, client, monkeypatch):
    mystery = workspace / "mystery.xyz"
    mystery.write_bytes(b"data")
    fake = use_model(monkeypatch, lambda _body: answer("Images", "mystery-picture.xyz"))
    first = scan(client, workspace)
    item = first["items"][0]
    assert (item["classification_source"], item["category"], item["proposed_name"]) == ("ollama", "images", "mystery-picture.xyz")
    assert first["processing"]["model_requests"] == 1

    action = plan(client, workspace, mystery)["actions"][0]
    assert action["source_type"] == "ollama"
    assert action["category"] == "images"
    assert Path(action["proposed_destination"]) == workspace.resolve() / "images" / "mystery-picture.xyz"
    assert action["proposed_name"] == "mystery-picture.xyz"
    assert "Ollama (m1)" in action["reason"]
    assert action["status"] == "pending" and action["validation"] == "warning"
    assert mystery.exists()  # planning never moves anything
    assert len(fake.generate_calls) == 1


def test_cached_model_answer_is_reused_without_new_requests_and_keeps_its_details(workspace, client, monkeypatch):
    (workspace / "mystery.xyz").write_bytes(b"data")
    fake = use_model(monkeypatch, lambda _body: answer("audio", "tune.xyz", "Sounds like audio."))
    scan(client, workspace)
    second = scan(client, workspace)
    item = second["items"][0]
    assert second["processing"]["cache_hits"] == 1 and second["processing"]["model_requests"] == 0
    assert (item["classification_source"], item["category"], item["proposed_name"]) == ("cache:ollama", "audio", "tune.xyz")
    assert item["classification_reason"] == "Sounds like audio."
    assert len(fake.generate_calls) == 1


def test_plan_ignores_a_suggestion_when_the_file_changed_after_the_scan(workspace, client, monkeypatch):
    mystery = workspace / "mystery.xyz"
    mystery.write_bytes(b"data")
    use_model(monkeypatch, lambda _body: answer("images", "x.xyz"))
    scan(client, workspace)
    mystery.write_bytes(b"different length now")
    action = plan(client, workspace, mystery)["actions"][0]
    assert action["source_type"] == "local-rule"
    assert action["category"] == "other"
    assert Path(action["proposed_destination"]) == workspace.resolve() / "other" / "mystery.xyz"


def test_plan_without_a_scan_uses_the_deterministic_rule(workspace, client):
    mystery = workspace / "mystery.xyz"
    mystery.write_bytes(b"data")
    action = plan(client, workspace, mystery)["actions"][0]
    assert action["source_type"] == "local-rule" and action["category"] == "other"


def test_editing_a_model_proposal_keeps_its_category_and_voids_approval(workspace, client, monkeypatch):
    mystery = workspace / "mystery.xyz"
    mystery.write_bytes(b"data")
    use_model(monkeypatch, lambda _body: answer("images", "model-name.xyz"))
    scan(client, workspace)
    created = plan(client, workspace, mystery)
    action = created["actions"][0]
    assert client.post(f"/api/plans/{created['id']}/approve", json={"action_ids": [action["id"]]}).status_code == 200
    edited = client.patch(f"/api/plans/{created['id']}/actions/{action['id']}", json={"proposed_name": "mine.xyz"}).json()["actions"][0]
    assert edited["status"] == "pending"
    assert Path(edited["proposed_destination"]) == workspace.resolve() / "images" / "mine.xyz"
    assert edited["source_type"] == "ollama"


def test_model_answers_are_never_used_for_easy_extensions_or_when_ai_is_off(workspace, client, monkeypatch):
    (workspace / "notes.txt").write_text("x", encoding="utf-8")
    (workspace / "mystery.xyz").write_bytes(b"data")
    fake = use_model(monkeypatch, lambda _body: answer())
    off = scan(client, workspace, use_ai=False)
    assert off["processing"]["model_requests"] == 0 and not fake.generate_calls
    sources = {i["name"]: i["classification_source"] for i in off["items"]}
    assert sources == {"notes.txt": "local-rule", "mystery.xyz": "local-rule"}
    on = scan(client, workspace)  # the rule-only answer is upgraded now that a model is allowed
    assert on["processing"]["model_requests"] == 1
    assert len(fake.generate_calls) == 1
    assert "notes.txt" not in json.dumps(fake.generate_calls[0]["prompt"]).replace("mystery.xyz", "")


# ---- invalid, unsafe and unavailable models fall back deterministically --------------

BAD_BODIES = {
    "malformed_json": lambda _b: {"response": "{not json"},
    "response_not_string": lambda _b: {"response": 5},
    "json_list": lambda _b: {"response": json.dumps(["images"])},
    "body_not_object": lambda _b: [1, 2, 3],
    "garbage_http_body": lambda _b: b"\xff\xfe not json at all",
    "unknown_category": lambda _b: answer("../../etc", "a.xyz"),
    "empty_reason": lambda _b: answer("images", "a.xyz", "   "),
    "missing_keys": lambda _b: {"response": json.dumps({"category": "images"})},
    "path_traversal_name": lambda _b: answer("images", "../evil.xyz"),
    "folder_in_name": lambda _b: answer("images", "sub/dir.xyz"),
    "reserved_device_name": lambda _b: answer("images", "CON.xyz"),
    "illegal_characters": lambda _b: answer("images", "a:b.xyz"),
    "trailing_dot": lambda _b: answer("images", "name.xyz."),
}


@pytest.mark.parametrize("case", sorted(BAD_BODIES))
def test_bad_model_output_falls_back_to_the_rule_and_is_retried_next_scan(case, workspace, client, monkeypatch):
    mystery = workspace / "mystery.xyz"
    mystery.write_bytes(b"data")
    fake = use_model(monkeypatch, BAD_BODIES[case])
    result = scan(client, workspace)
    item = result["items"][0]
    assert item["classification_source"] == "local-rule" and item["category"] == "other"
    assert item["proposed_name"] == "mystery.xyz"
    assert result["processing"]["model_requests"] == 1 and result["processing"]["model_fallbacks"] == 1
    action = plan(client, workspace, mystery)["actions"][0]
    assert action["source_type"] == "local-rule" and action["category"] == "other"
    retry = scan(client, workspace)
    assert retry["processing"]["cache_hits"] == 0 and retry["processing"]["model_requests"] == 1
    assert len(fake.generate_calls) == 2


@pytest.mark.parametrize("error", [
    urllib.error.URLError("connection refused"),
    TimeoutError("timed out"),
    ConnectionResetError("reset"),
])
def test_unavailable_or_slow_model_falls_back(error, workspace, client, monkeypatch):
    (workspace / "mystery.xyz").write_bytes(b"data")

    def boom(_body):
        raise error

    use_model(monkeypatch, boom)
    result = scan(client, workspace)
    assert result["items"][0]["classification_source"] == "local-rule"
    assert result["processing"]["model_fallbacks"] == 1


def test_model_that_is_not_installed_is_never_called(workspace, client, monkeypatch):
    (workspace / "mystery.xyz").write_bytes(b"data")
    fake = use_model(monkeypatch, lambda _b: answer(), models=("other-model",), selected="m1")
    result = scan(client, workspace)
    assert result["processing"]["model_requests"] == 0 and not fake.generate_calls
    assert result["items"][0]["classification_source"] == "local-rule"


def test_unresponsive_model_is_abandoned_after_repeated_failures(workspace, client, monkeypatch):
    for index in range(6):
        (workspace / f"f{index}.xyz").write_bytes(b"data")

    def boom(_body):
        raise TimeoutError("timed out")

    fake = use_model(monkeypatch, boom)
    result = scan(client, workspace)
    assert len(fake.generate_calls) == main.MAX_CONSECUTIVE_MODEL_FAILURES
    assert result["processing"]["model_requests"] == main.MAX_CONSECUTIVE_MODEL_FAILURES
    assert result["processing"]["model_fallbacks"] == 6
    assert {i["classification_source"] for i in result["items"]} == {"local-rule"}


def test_per_scan_request_budget_defers_the_rest_to_the_next_scan(workspace, client, monkeypatch):
    monkeypatch.setattr(main, "MAX_CLASSIFICATION_BATCH", 2)
    for index in range(4):
        (workspace / f"f{index}.xyz").write_bytes(b"data")
    fake = use_model(monkeypatch, lambda _b: answer("code", "f.xyz"))
    first = scan(client, workspace)
    assert first["processing"]["model_requests"] == 2 and first["processing"]["model_fallbacks"] == 2
    second = scan(client, workspace)
    assert second["processing"]["cache_hits"] == 2 and second["processing"]["model_requests"] == 2
    assert len(fake.generate_calls) == 4
    third = scan(client, workspace)
    assert third["processing"]["cache_hits"] == 4 and third["processing"]["model_requests"] == 0


def test_changing_the_selected_model_invalidates_answers_from_the_old_one(workspace, client, monkeypatch):
    (workspace / "mystery.xyz").write_bytes(b"data")
    fake = use_model(monkeypatch, lambda _b: answer(), models=("m1", "m2"), selected="m1")
    scan(client, workspace)
    monkeypatch.setenv("OLLAMA_MODEL", "m2")
    again = scan(client, workspace)
    assert again["processing"]["reprocessed"] == 1 and again["processing"]["model_requests"] == 1
    assert [call["model"] for call in fake.generate_calls] == ["m1", "m2"]


def test_prompt_contains_metadata_only_and_requests_deterministic_json(workspace, client, monkeypatch):
    secret = workspace / "mystery.xyz"
    secret.write_bytes(b"TOP-SECRET-CONTENT")
    fake = use_model(monkeypatch, lambda _b: answer())
    scan(client, workspace)
    call = fake.generate_calls[0]
    assert "TOP-SECRET-CONTENT" not in json.dumps(call)
    assert call["format"] == "json" and call["stream"] is False and call["options"]["temperature"] == 0
    assert "mystery.xyz" in call["prompt"] and "size_bytes: 18" in call["prompt"]


def test_model_cannot_change_the_file_type(workspace, client, monkeypatch):
    (workspace / "mystery.xyz").write_bytes(b"data")
    use_model(monkeypatch, lambda _b: answer("code", "malware.exe"))
    item = scan(client, workspace)["items"][0]
    assert item["classification_source"] == "ollama" and item["proposed_name"] == "mystery.xyz"


@pytest.mark.parametrize("name", ["", ".", "..", "a/b", "a\\b", "con", "NUL.txt", "lpt1.log", "a:b", "a*b", "a?b", 'a"b', "a<b", "a|b", " lead.txt", "trail.", "trail ", "x" * 256, "bad\x01.txt"])
def test_unsafe_names_are_rejected(name):
    assert main.unsafe_name_reason(name)


@pytest.mark.parametrize("name", ["report.pdf", "my file (1).txt", "résumé.docx", "a.b.c", "x" * 255, "comma,name.txt"])
def test_ordinary_names_are_accepted(name):
    assert main.unsafe_name_reason(name) is None


def test_unsafe_user_edit_is_blocked_by_the_expanded_name_rules(workspace, client):
    source = workspace / "notes.txt"
    source.write_text("x", encoding="utf-8")
    created = plan(client, workspace, source)
    action_id = created["actions"][0]["id"]
    for bad in ("CON.txt", "a:b.txt", "trailing.", "bad|name.txt"):
        edited = client.patch(f"/api/plans/{created['id']}/actions/{action_id}", json={"proposed_name": bad}).json()["actions"][0]
        assert edited["validation"] == "blocked", bad
        assert client.post(f"/api/plans/{created['id']}/approve", json={"action_ids": [action_id]}).status_code == 409

"""Model inference -> cache -> review plan, with the local model fully mocked (no real Ollama is contacted)."""
import json
import urllib.error
from pathlib import Path

import pytest

from app import main
from model_fakes import FakeOllama, Response, answer, client, plan, scan, use_model, workspace  # noqa: F401
from apply_helpers import apply_moves  # noqa: E402


# ---- the model's valid answer reaches the review plan ------------------------------

def test_valid_model_category_reaches_the_plan_but_the_file_name_never_changes(workspace, client, monkeypatch):
    mystery = workspace / "mystery.xyz"
    mystery.write_bytes(b"data")
    fake = use_model(monkeypatch, lambda _body: answer("Images", "mystery-picture.xyz"))
    first = scan(client, workspace)
    item = first["items"][0]
    assert (item["classification_source"], item["category"]) == ("ollama", "images")
    assert item["proposed_name"] == "mystery.xyz"  # the original name is kept
    assert item["suggested_name"] == "mystery-picture.xyz"  # offered only as a suggestion
    assert item["classification_basis"] == "filename-and-size"
    assert first["processing"]["model_requests"] == 1

    action = plan(client, workspace, mystery)["actions"][0]
    assert action["source_type"] == "ollama" and action["category"] == "images"
    assert action["proposed_name"] == "mystery.xyz" and action["name_source"] == "original"
    assert action["suggested_name"] == "mystery-picture.xyz"
    assert Path(action["proposed_destination"]) == workspace.resolve() / "images" / "mystery.xyz"
    assert action["explanation_basis"] == "filename-and-size" and action["model"] == "m1"
    assert action["status"] == "pending" and action["validation"] == "warning"
    assert mystery.exists()  # planning never moves anything
    assert len(fake.generate_calls) == 1


def test_applying_the_model_suggestion_is_an_explicit_opt_in_that_still_needs_approval(workspace, client, monkeypatch):
    mystery = workspace / "mystery.xyz"
    mystery.write_bytes(b"data")
    use_model(monkeypatch, lambda _body: answer("images", "mystery-picture.xyz"))
    scan(client, workspace)
    created = plan(client, workspace, mystery)
    action = created["actions"][0]
    assert client.post(f"/api/plans/{created['id']}/approve", json={"action_ids": [action["id"]]}).status_code == 200
    applied = client.patch(f"/api/plans/{created['id']}/actions/{action['id']}", json={"use_suggestion": True})
    assert applied.status_code == 200
    updated = applied.json()["actions"][0]
    assert updated["proposed_name"] == "mystery-picture.xyz" and updated["name_source"] == "model-suggestion"
    assert updated["status"] == "pending"  # the earlier approval was voided by the edit
    assert apply_moves(client, created["id"], [action["id"]]).status_code == 409
    assert mystery.exists()
    assert client.post(f"/api/plans/{created['id']}/approve", json={"action_ids": [action["id"]]}).status_code == 200
    assert apply_moves(client, created["id"], [action["id"]]).status_code == 200
    assert (workspace / "images" / "mystery-picture.xyz").exists() and not mystery.exists()


def test_a_plan_without_a_suggestion_cannot_apply_one_and_requests_must_be_unambiguous(workspace, client):
    note = workspace / "notes.txt"
    note.write_text("x", encoding="utf-8")
    created = plan(client, workspace, note)
    path = f"/api/plans/{created['id']}/actions/{created['actions'][0]['id']}"
    assert client.patch(path, json={"use_suggestion": True}).status_code == 409
    assert client.patch(path, json={}).status_code == 422
    assert client.patch(path, json={"proposed_name": "x.txt", "use_suggestion": True}).status_code == 422


def test_cached_model_answer_is_reused_without_new_requests_and_keeps_its_details(workspace, client, monkeypatch):
    (workspace / "mystery.xyz").write_bytes(b"data")
    fake = use_model(monkeypatch, lambda _body: answer("audio", "tune.xyz", "Sounds like audio."))
    scan(client, workspace)
    second = scan(client, workspace)
    item = second["items"][0]
    assert second["processing"]["cache_hits"] == 1 and second["processing"]["model_requests"] == 0
    assert (item["classification_source"], item["category"], item["proposed_name"]) == ("cache:ollama", "audio", "mystery.xyz")
    assert item["suggested_name"] == "tune.xyz" and item["model_note"] == "Sounds like audio."
    assert "file name and size only" in item["classification_reason"] and "unverified" in item["classification_reason"]
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


# The model replied but the answer itself is unusable: repeatable for the same input, so it becomes a FINAL rule result.
# Protocol-level problems (the HTTP reply is not a usable envelope) say nothing about the answer and are retried.
PROTOCOL_LEVEL = {"response_not_string", "body_not_object", "garbage_http_body"}
REJECTED_ANSWERS = sorted(set(BAD_BODIES) - PROTOCOL_LEVEL)


@pytest.mark.parametrize("case", REJECTED_ANSWERS)
def test_a_rejected_model_answer_becomes_a_final_rule_result_and_is_not_requested_again(case, workspace, client, monkeypatch):
    mystery = workspace / "mystery.xyz"
    mystery.write_bytes(b"data")
    fake = use_model(monkeypatch, BAD_BODIES[case])
    result = scan(client, workspace)
    item = result["items"][0]
    assert item["classification_source"] == "local-rule" and item["category"] == "other"
    assert item["proposed_name"] == "mystery.xyz" and item["suggested_name"] is None
    assert result["processing"]["model_requests"] == 1 and result["processing"]["model_fallbacks"] == 1
    assert "rejected by validation" in item["classification_reason"] and item["model_rejection"]
    action = plan(client, workspace, mystery)["actions"][0]
    assert action["source_type"] == "local-rule" and action["category"] == "other" and action["suggested_name"] is None
    for _ in range(3):  # every later scan of the unchanged file is a cache hit: no new request, still a rule result
        again = scan(client, workspace)
        assert again["processing"]["cache_hits"] == 1 and again["processing"]["model_requests"] == 0
        assert again["processing"]["rejected_cache_hits"] == 1 and again["processing"]["model_fallbacks"] == 0
        cached = again["items"][0]
        assert cached["classification_source"] == "cache:local-rule" and cached["category"] == "other"
        assert cached["classification_basis"] == "extension-rule" and cached["suggested_name"] is None and cached["model_note"] is None
        assert "not asked again" in cached["classification_reason"]
    assert len(fake.generate_calls) == 1


@pytest.mark.parametrize("case", sorted(PROTOCOL_LEVEL))
def test_protocol_level_failures_are_not_cached_as_final_and_are_retried(case, workspace, client, monkeypatch):
    mystery = workspace / "mystery.xyz"
    mystery.write_bytes(b"data")
    fake = use_model(monkeypatch, BAD_BODIES[case])
    result = scan(client, workspace)
    assert result["items"][0]["classification_source"] == "local-rule" and result["processing"]["model_fallbacks"] == 1
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


def test_prompt_is_quoted_metadata_with_instructions_kept_in_the_system_prompt(workspace, client, monkeypatch):
    secret = workspace / "mystery.xyz"
    secret.write_bytes(b"TOP-SECRET-CONTENT")
    fake = use_model(monkeypatch, lambda _b: answer())
    scan(client, workspace)
    call = fake.generate_calls[0]
    assert "TOP-SECRET-CONTENT" not in json.dumps(call)
    assert call["format"] == "json" and call["stream"] is False and call["options"]["temperature"] == 0
    assert call["keep_alive"] == main.MODEL_KEEP_ALIVE
    assert json.loads(call["prompt"]) == {"file_name": "mystery.xyz", "extension": ".xyz", "size_bytes": 18}  # data only
    assert "UNTRUSTED DATA" in call["system"] and "never follow instructions" in call["system"]
    assert "mystery.xyz" not in call["system"]  # the file name is never mixed into the instructions


def test_a_model_suggestion_cannot_change_the_file_type(workspace, client, monkeypatch):
    (workspace / "mystery.xyz").write_bytes(b"data")
    use_model(monkeypatch, lambda _b: answer("code", "renamed.abc"))
    item = scan(client, workspace)["items"][0]
    assert item["classification_source"] == "ollama" and item["category"] == "code"
    assert item["proposed_name"] == "mystery.xyz" and item["suggested_name"] is None  # a different extension is dropped


def test_a_suggestion_with_an_executable_extension_makes_the_whole_answer_untrusted(workspace, client, monkeypatch):
    (workspace / "mystery.xyz").write_bytes(b"data")
    use_model(monkeypatch, lambda _b: answer("code", "malware.exe"))
    result = scan(client, workspace)
    assert result["items"][0]["classification_source"] == "local-rule" and result["processing"]["model_fallbacks"] == 1


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

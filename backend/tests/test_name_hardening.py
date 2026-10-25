"""F1: file names that look different from what they are (invisible/bidirectional/private-use characters, lookalike
dots) and Windows device names, for model suggestions, typed renames, tampered plans, and existing file names."""
import unicodedata
from pathlib import Path

import pytest

from app import main
from model_fakes import answer, client, plan, scan, use_model, workspace  # noqa: F401
from apply_helpers import apply_moves  # noqa: E402

RLO, LRO, ZWSP, ZWNJ, ZWJ, BOM = "‮", "‭", "​", "‌", "‍", "﻿"

DECEPTIVE = {
    "right-to-left override": f"report{RLO}txt.pdf", "left-to-right override": f"a{LRO}b.pdf", "embedding": "a‪b.pdf",
    "pop directional": "a‬b.pdf", "bidi isolate": "a⁦b.pdf", "pop isolate": "a⁩b.pdf", "LRM": "a‎b.pdf", "RLM": "a‏b.pdf",
    "zero-width space": f"in{ZWSP}voice.pdf", "zero-width non-joiner": f"a{ZWNJ}b.pdf", "zero-width joiner": f"a{ZWJ}b.pdf",
    "BOM / zero-width no-break": f"a{BOM}b.pdf", "word joiner": "a⁠b.pdf", "soft hyphen": "a­b.pdf",
    "line separator": "a b.pdf", "paragraph separator": "a b.pdf", "private use": "ab.pdf",
    "unassigned": "a͸b.pdf", "tag character": "a\U000e0041b.pdf", "lone surrogate": "a\ud800b.pdf",
}
LEGITIMATE = ["Résumé 2024.pdf", "財務報告.pdf", "Отчёт.pdf", "ملف.pdf", "report (final) [v2].pdf", "naïve café.pdf", "emoji 🎉.pdf", "a.b.c.pdf", "x" * 200 + ".pdf"]


def write(root: Path, name: str, content: bytes = b"synthetic") -> Path:
    path = root / name
    path.write_bytes(content)
    return path


@pytest.mark.parametrize("label", sorted(DECEPTIVE))
def test_deceptive_characters_are_detected_with_their_code_point(label):
    reason = main.deceptive_character(DECEPTIVE[label])
    assert reason and "U+" in reason and "invisible" in reason


@pytest.mark.parametrize("name", LEGITIMATE)
def test_ordinary_international_names_are_not_flagged(name):
    assert main.deceptive_character(name) is None and main.unsafe_name_reason(name) is None


def test_every_flagged_category_really_is_one_of_the_documented_ones():
    for name in DECEPTIVE.values():
        categories = {unicodedata.category(character) for character in name}
        assert categories & main.INVISIBLE_CATEGORIES


# ---- Windows device names, including the variants found in the review ------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "COM¹.pdf", "COM².pdf", "COM³.txt", "LPT¹", "lpt².doc", "CONIN$", "conin$.pdf", "CONOUT$.txt", "CONOUT$",
    "CON.pdf", "con", "NUL", "aux.tar.gz", "COM1", "COM9.txt", "LPT9.log", "PRN.pdf",
    "CON .pdf", "NUL  .txt",                                 # Windows ignores trailing spaces in the base name
    "ＣＯＮ.pdf", "COM１.pdf",               # fullwidth lookalikes fold to CON / COM1 under NFKC
])
def test_windows_device_names_are_refused_in_every_spelling(name):
    assert main.unsafe_name_reason(name) == "The proposed filename is a reserved Windows device name."


@pytest.mark.parametrize("name", ["CONSOLE.pdf", "COMMON.txt", "console.log", "aux2.pdf", "nullable.txt", "COM10.txt", "LPT0x.txt", "icon$.pdf"])
def test_names_that_merely_start_like_a_device_name_are_fine(name):
    assert main.unsafe_name_reason(name) is None


# ---- suggestions from a model ------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("label", sorted(DECEPTIVE))
def test_a_model_suggestion_with_deceptive_characters_makes_the_whole_answer_untrusted(workspace, client, monkeypatch, label):
    path = write(workspace, "mystery.xyz")
    suggestion = DECEPTIVE[label].replace(".pdf", ".xyz")
    use_model(monkeypatch, lambda _b: answer("images", suggestion))
    result = scan(client, workspace)
    item = result["items"][0]
    assert item["classification_source"] == "local-rule" and item["suggested_name"] is None
    assert "invisible" in item["classification_reason"] and "rejected" in item["classification_reason"]
    action = plan(client, workspace, path)["actions"][0]
    assert action["suggested_name"] is None and action["proposed_name"] == "mystery.xyz"
    assert scan(client, workspace)["processing"]["model_requests"] == 0  # a rejection is remembered, like any other


def test_a_lookalike_dot_cannot_smuggle_an_executable_extension(workspace, client, monkeypatch):
    path = write(workspace, "mystery.xyz")
    for name in ("setup．exe", "setup．bat.xyz", "run․cmd.xyz"):
        problem = main.suggestion_problem(path, name)
        assert problem, name
    assert main.suggestion_problem(path, "setup．bat.xyz") == "the suggestion contains an executable or script extension"


def test_a_clean_international_suggestion_is_still_accepted(workspace, client, monkeypatch):
    path = write(workspace, "mystery.xyz")
    use_model(monkeypatch, lambda _b: answer("documents", "Résumé 2024.xyz"))
    item = scan(client, workspace)["items"][0]
    assert item["classification_source"] == "ollama" and item["suggested_name"] == "Résumé 2024.xyz"


# ---- names typed by the user -----------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("label", ["right-to-left override", "zero-width space", "private use", "BOM / zero-width no-break"])
def test_a_typed_rename_with_deceptive_characters_is_blocked_and_cannot_be_approved(workspace, client, label):
    note = write(workspace, "notes.txt")
    created = plan(client, workspace, note)
    action = created["actions"][0]
    url = f"/api/plans/{created['id']}/actions/{action['id']}"
    edited = client.patch(url, json={"proposed_name": DECEPTIVE[label].replace(".pdf", ".txt")}).json()["actions"][0]
    assert edited["validation"] == "blocked" and "invisible" in edited["reason"]
    assert client.post(f"/api/plans/{created['id']}/approve", json={"action_ids": [action["id"]]}).status_code == 409
    assert note.exists()


def test_a_typed_device_name_is_blocked(workspace, client):
    note = write(workspace, "notes.txt")
    created = plan(client, workspace, note)
    action = created["actions"][0]
    for name in ("COM¹.txt", "CONIN$", "CON .txt"):
        edited = client.patch(f"/api/plans/{created['id']}/actions/{action['id']}", json={"proposed_name": name}).json()["actions"][0]
        assert edited["validation"] == "blocked" and "device name" in edited["reason"]


# ---- tampered plans ----------------------------------------------------------------------------------------------------------------------

def test_a_tampered_destination_name_with_deceptive_characters_is_refused_at_apply(workspace, client):
    note = write(workspace, "notes.txt")
    created = plan(client, workspace, note)
    action = created["actions"][0]
    assert client.post(f"/api/plans/{created['id']}/approve", json={"action_ids": [action["id"]]}).status_code == 200
    connection = main.db()
    _, actions = main.load_plan(connection, created["id"])
    actions[0].proposed_destination = str(workspace.resolve() / "documents" / f"notes{RLO}fdp.txt")
    actions[0].name_source = "user"  # claims the user typed it; deceptive characters are refused regardless
    main.save_plan(connection, created["id"], actions)
    connection.close()
    result = apply_moves(client, created["id"], [action["id"]])
    assert result.status_code == 409 and "invisible" in result.json()["detail"] and note.exists()


# ---- file names that already exist -----------------------------------------------------------------------------------------------------------

def test_an_existing_file_with_a_deceptive_name_is_flagged_but_can_still_be_organized(workspace, client):
    spoofed = write(workspace, f"invoice{RLO}fdp.txt")
    created = plan(client, workspace, spoofed)
    action = created["actions"][0]
    assert action["validation"] == "warning" and "invisible or direction-control" in action["reason"]
    assert action["proposed_name"] == spoofed.name  # never renamed behind the user's back
    assert client.post(f"/api/plans/{created['id']}/approve", json={"action_ids": [action["id"]]}).status_code == 200
    assert apply_moves(client, created["id"], [action["id"]]).status_code == 200
    assert (workspace / "documents" / spoofed.name).exists()


def test_an_existing_file_with_a_legitimate_international_name_is_unaffected(workspace, client):
    path = write(workspace, "財務報告.pdf")
    action = plan(client, workspace, path)["actions"][0]
    assert action["validation"] == "warning" and "invisible" not in action["reason"]

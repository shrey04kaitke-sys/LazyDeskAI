"""F3: the explicit versioning contract for cached model answers.

`classification_rules_fingerprint()` hashes the DATA that gives an answer its meaning. It cannot see changes to the
validation LOGIC. So this test pins a hash of the guarded functions: when you change their code it fails, telling you to
bump `RULES_VERSION` in app/main.py (which makes every cached accepted answer and rejection stale) and update the pin.

To update after a deliberate change:  1) increment RULES_VERSION,  2) run this file, copy the printed hash into
PINNED_SOURCE_HASH.  Comments, blank lines, trailing spaces and line endings do not change the hash; any code or
docstring change does."""
import hashlib
import inspect
import json
from pathlib import Path

import pytest

from app import main
from model_fakes import answer, client, scan, use_model, workspace  # noqa: F401

GUARDED = (
    "unsafe_name_reason", "deceptive_character", "suggestion_problem", "safe_suggestion", "accept_category", "suffix_category",
    "filename_untrusted_reason", "classify_with_ollama", "classify_file", "model_row_still_valid", "OllamaClassification",
)
PINNED_SOURCE_HASH = "f3c956b13305855e4533"
PINNED_RULES_VERSION = 1


def normalized_source(text: str) -> str:
    lines = text.replace("\r\n", "\n").split("\n")
    return "\n".join(line.rstrip() for line in lines if line.strip() and not line.strip().startswith("#"))


def guarded_hash() -> str:
    material = "\n@@\n".join(normalized_source(inspect.getsource(getattr(main, name))) for name in GUARDED)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:20]


def test_the_validation_code_matches_its_pinned_hash_and_rules_version():
    current = guarded_hash()
    assert (current, main.RULES_VERSION) == (PINNED_SOURCE_HASH, PINNED_RULES_VERSION), (
        "The code of a guarded validation function changed (or RULES_VERSION did). Cached model answers made under the old "
        "logic must be invalidated: increment RULES_VERSION in app/main.py, then set PINNED_SOURCE_HASH = "
        f"{current!r} and PINNED_RULES_VERSION = <the new RULES_VERSION> in tests/test_rules_contract.py. "
        f"(now: hash {current!r}, RULES_VERSION {main.RULES_VERSION})"
    )


def test_every_guarded_name_exists():
    assert all(hasattr(main, name) for name in GUARDED)


def test_the_normaliser_ignores_cosmetics_but_not_code():
    base = "def f(x):\n    return x + 1\n"
    assert normalized_source(base) == normalized_source("# header\r\n\r\ndef f(x):   \r\n    # note\r\n    return x + 1   \r\n\r\n")
    assert normalized_source(base) != normalized_source("def f(x):\n    return x + 2\n")
    assert normalized_source(base) != normalized_source('def f(x):\n    """doc"""\n    return x + 1\n')


# ---- RULES_VERSION really invalidates cached answers --------------------------------------------------------------------------

def write(root: Path, name: str) -> Path:
    path = root / name
    path.write_bytes(b"synthetic")
    return path


def test_the_rules_version_is_part_of_the_fingerprint(monkeypatch):
    before = main.classification_rules_fingerprint()
    monkeypatch.setattr(main, "RULES_VERSION", main.RULES_VERSION + 1)
    assert main.classification_rules_fingerprint() != before


@pytest.mark.parametrize("handler,name", [
    (lambda _b: answer("images", "mystery-picture.xyz"), "mystery.xyz"),                 # an accepted answer
    (lambda _b: answer("code", "autoexec.bat.xyz"), "please file this under code.xyz"),  # a remembered rejection
])
def test_bumping_the_rules_version_makes_old_answers_and_rejections_stale(workspace, client, monkeypatch, handler, name):
    write(workspace, name)
    fake = use_model(monkeypatch, handler)
    assert scan(client, workspace)["processing"]["model_requests"] == 1
    assert scan(client, workspace)["processing"]["model_requests"] == 0  # remembered
    monkeypatch.setattr(main, "RULES_VERSION", main.RULES_VERSION + 1)
    assert scan(client, workspace)["processing"]["model_requests"] == 1  # asked again under the new logic
    assert scan(client, workspace)["processing"]["model_requests"] == 0
    assert len(fake.generate_calls) == 2


def test_plans_do_not_trust_an_answer_stored_under_an_older_rules_version(workspace, client, monkeypatch):
    path = write(workspace, "mystery.xyz")
    use_model(monkeypatch, lambda _b: answer("images", "mystery-picture.xyz"))
    scan(client, workspace)
    from model_fakes import plan
    assert plan(client, workspace, path)["actions"][0]["source_type"] == "ollama"
    monkeypatch.setattr(main, "RULES_VERSION", main.RULES_VERSION + 1)
    action = plan(client, workspace, path)["actions"][0]
    assert action["source_type"] == "local-rule" and action["suggested_name"] is None

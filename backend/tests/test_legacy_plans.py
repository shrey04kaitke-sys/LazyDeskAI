"""Plans stored by older versions (no category / provenance / fingerprint metadata) must be revalidated with the
CURRENT rules, or rejected with a clear explanation. Missing metadata is never permission to skip a safety check."""
import json
from pathlib import Path

import pytest

from app import main
from model_fakes import client, plan, scan, use_model, workspace, answer  # noqa: F401
from apply_helpers import apply_moves  # noqa: E402

NEW_FIELDS = ("category", "suggested_name", "name_source", "explanation_basis", "model", "model_note", "plan_schema")


def write(root: Path, name: str, content: bytes = b"synthetic") -> Path:
    path = root / name
    path.write_bytes(content)
    return path


def rewrite_plan(plan_id: str, legacy: bool = True, keep_fingerprint: bool = True, **overrides):
    """Turn a freshly created plan into what an older version would have stored."""
    connection = main.db()
    row = connection.execute("SELECT actions FROM plans WHERE id = ?", (plan_id,)).fetchone()
    items = json.loads(row["actions"])
    for item in items:
        if legacy:
            for key in NEW_FIELDS:
                item.pop(key, None)
        if not keep_fingerprint:
            item["source_fingerprint"] = None
        item.update(overrides)
    connection.execute("UPDATE plans SET actions = ? WHERE id = ?", (json.dumps(items), plan_id))
    connection.commit()
    connection.close()


def make(workspace, client, name="notes.txt"):
    source = write(workspace, name)
    created = plan(client, workspace, source)
    return source, created, created["actions"][0]


def approve(client, created, action):
    return client.post(f"/api/plans/{created['id']}/approve", json={"action_ids": [action["id"]]})


def execute(client, created, action):
    return apply_moves(client, created["id"], [action["id"]])


def force_approved(plan_id: str) -> None:
    """Simulate a plan that was already approved (for example by an older version) before it is applied."""
    connection = main.db()
    items = json.loads(connection.execute("SELECT actions FROM plans WHERE id = ?", (plan_id,)).fetchone()["actions"])
    for item in items:
        item["status"] = "approved"
    connection.execute("UPDATE plans SET actions = ? WHERE id = ?", (json.dumps(items), plan_id))
    connection.commit()
    connection.close()


def assert_refused(client, created, action, source, text=None):
    """Approval is refused with a reason, AND an already-approved copy of the same plan is refused at apply time."""
    approval = approve(client, created, action)
    assert approval.status_code == 409, approval.text
    force_approved(created["id"])
    applied = execute(client, created, action)
    assert applied.status_code == 409, applied.text
    if text:
        assert text in approval.json()["detail"] and text in applied.json()["detail"], (approval.json(), applied.json())
    assert source.exists()
    moved = [p for p in source.parent.rglob("*") if p.is_file() and p != source and p.name == source.name]
    assert not moved


# ---- the stored shape really is "legacy" --------------------------------------------------------

def test_a_legacy_plan_loads_with_defaults_and_is_flagged_as_legacy(workspace, client):
    source, created, action = make(workspace, client)
    rewrite_plan(created["id"])
    loaded = main.load_plan(main.db(), created["id"])[1][0]
    assert (loaded.category, loaded.plan_schema, loaded.name_source) == ("", 0, "original")
    assert main.PLAN_SCHEMA_VERSION > 0


# ---- missing category metadata can no longer skip the folder / extension checks -----------------

@pytest.mark.parametrize("legacy", [True, False], ids=["legacy-plan", "blanked-category-on-a-current-plan"])
def test_a_blank_category_cannot_move_a_file_into_an_arbitrary_folder(workspace, client, legacy):
    source, created, action = make(workspace, client)
    rewrite_plan(created["id"], legacy=legacy, category="", proposed_destination=str(workspace.resolve() / "anywhere" / "notes.txt"))
    assert_refused(client, created, action, source, "category")
    assert not (workspace / "anywhere").exists()


@pytest.mark.parametrize("legacy", [True, False])
def test_a_blank_category_cannot_move_a_file_up_to_the_workspace_root(workspace, client, legacy):
    nested = workspace / "sub"
    nested.mkdir()
    source = write(nested, "notes.txt")
    created = plan(client, workspace, source)
    action = created["actions"][0]
    rewrite_plan(created["id"], legacy=legacy, category="", proposed_destination=str(workspace.resolve() / "notes.txt"))
    assert_refused(client, created, action, source)


def test_a_legacy_destination_that_contradicts_the_extension_rule_is_refused(workspace, client):
    source, created, action = make(workspace, client)
    rewrite_plan(created["id"], proposed_destination=str(workspace.resolve() / "code" / "notes.txt"))  # a .txt is never "code"
    assert_refused(client, created, action, source, "category")


def test_a_legacy_unknown_extension_file_is_checked_against_the_other_folder(workspace, client):
    source, created, action = make(workspace, client, "mystery.xyz")
    rewrite_plan(created["id"], proposed_destination=str(workspace.resolve() / "documents" / "mystery.xyz"))
    assert_refused(client, created, action, source, "category")


# ---- missing fingerprint: revalidated or rejected, never skipped -------------------------------

def test_a_legacy_plan_without_a_content_fingerprint_is_rejected_with_a_clear_explanation(workspace, client):
    source, created, action = make(workspace, client)
    rewrite_plan(created["id"], keep_fingerprint=False)
    assert_refused(client, created, action, source, "older version")
    detail = approve(client, created, action).json()["detail"]
    assert "create a new plan" in detail


def test_a_legacy_plan_whose_file_cannot_be_hashed_is_still_revalidated_by_size_and_mtime(workspace, client, monkeypatch):
    source, created, action = make(workspace, client)
    rewrite_plan(created["id"], keep_fingerprint=False)
    monkeypatch.setattr(main, "is_cloud_placeholder", lambda _path: True)  # current rules never hash cloud-only files
    assert approve(client, created, action).status_code == 200
    assert execute(client, created, action).status_code == 200
    assert (workspace / "documents" / "notes.txt").exists() and not source.exists()


# ---- a legacy plan that passes every current rule still works (compatibility) --------------------

def test_a_valid_legacy_plan_is_revalidated_and_can_be_approved_and_applied(workspace, client):
    source, created, action = make(workspace, client)
    rewrite_plan(created["id"])  # keeps the fingerprint
    assert approve(client, created, action).status_code == 200
    assert execute(client, created, action).status_code == 200
    assert (workspace / "documents" / "notes.txt").read_bytes() == b"synthetic" and not source.exists()


def test_a_legacy_plan_stored_as_approved_is_still_revalidated_at_apply_time(workspace, client):
    source, created, action = make(workspace, client)
    rewrite_plan(created["id"], status="approved")
    source.write_bytes(b"changed after approval!")  # different size
    assert execute(client, created, action).status_code == 409 and source.exists()


# ---- every other check still applies to legacy plans ---------------------------------------------

def test_a_legacy_plan_still_needs_approval(workspace, client):
    source, created, action = make(workspace, client)
    rewrite_plan(created["id"])
    assert execute(client, created, action).status_code == 409 and source.exists()
    client.post(f"/api/plans/{created['id']}/reject", json={"action_ids": [action["id"]]})
    assert execute(client, created, action).status_code == 409


def test_a_legacy_plan_with_a_changed_source_is_stale(workspace, client):
    source, created, action = make(workspace, client)
    rewrite_plan(created["id"], status="approved")
    source.write_bytes(b"x")
    assert execute(client, created, action).status_code == 409


def test_a_legacy_plan_missing_size_and_mtime_metadata_is_refused(workspace, client):
    source, created, action = make(workspace, client)
    connection = main.db()
    rewrite_plan(created["id"], status="approved")
    row = connection.execute("SELECT actions FROM plans WHERE id = ?", (created["id"],)).fetchone()
    items = json.loads(row["actions"])
    for item in items:
        item.pop("source_size", None); item.pop("source_mtime_ns", None)
    connection.execute("UPDATE plans SET actions = ? WHERE id = ?", (json.dumps(items), created["id"])); connection.commit(); connection.close()
    assert execute(client, created, action).status_code == 409 and source.exists()


def test_a_legacy_plan_without_a_workspace_root_is_refused(workspace, client):
    source, created, action = make(workspace, client)
    rewrite_plan(created["id"], status="approved", workspace_root="")
    assert execute(client, created, action).status_code == 409 and source.exists()


def test_a_legacy_destination_outside_the_workspace_is_refused(workspace, client, tmp_path):
    source, created, action = make(workspace, client)
    rewrite_plan(created["id"], status="approved", proposed_destination=str(tmp_path / "documents" / "notes.txt"))
    assert execute(client, created, action).status_code == 409
    assert not (tmp_path / "documents").exists()


def test_a_legacy_plan_with_an_unsafe_destination_name_is_refused(workspace, client):
    source, created, action = make(workspace, client)
    rewrite_plan(created["id"], status="approved", proposed_destination=str(workspace.resolve() / "documents" / "CON.txt"))
    assert execute(client, created, action).status_code == 409 and source.exists()


def test_a_legacy_plan_whose_source_became_a_symlink_is_refused(workspace, client, tmp_path):
    source, created, action = make(workspace, client)
    rewrite_plan(created["id"], status="approved")
    target = write(tmp_path, "outside.txt")
    source.unlink()
    try:
        source.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this system")
    assert execute(client, created, action).status_code == 409 and target.exists()


def test_a_legacy_plan_in_a_destination_conflict_is_refused(workspace, client):
    source, created, action = make(workspace, client)
    rewrite_plan(created["id"], status="approved")
    (workspace / "documents").mkdir()
    (workspace / "documents" / "notes.txt").write_text("existing", encoding="utf-8")
    assert execute(client, created, action).status_code == 409
    assert (workspace / "documents" / "notes.txt").read_text(encoding="utf-8") == "existing"


# ---- name provenance: a rename must be explained by how the name was chosen ----------------------

def test_a_destination_name_that_differs_without_a_recorded_rename_is_refused(workspace, client):
    source, created, action = make(workspace, client)
    rewrite_plan(created["id"], legacy=False, proposed_destination=str(workspace.resolve() / "documents" / "other-name.txt"))
    assert_refused(client, created, action, source, "no rename was requested")


def test_a_model_suggested_name_is_rechecked_when_applying(workspace, client):
    source, created, action = make(workspace, client)
    rewrite_plan(created["id"], legacy=False, name_source="model-suggestion", proposed_name="autoexec.bat.txt",
                 proposed_destination=str(workspace.resolve() / "documents" / "autoexec.bat.txt"))
    assert_refused(client, created, action, source, "model-suggested name")


def test_a_legacy_model_proposed_dangerous_name_is_refused(workspace, client):
    source, created, action = make(workspace, client)
    rewrite_plan(created["id"], source_type="ollama", proposed_destination=str(workspace.resolve() / "documents" / "run.bat.txt"))
    assert_refused(client, created, action, source, "model-suggested name")


def test_a_name_the_user_chose_is_still_allowed(workspace, client):
    source, created, action = make(workspace, client)
    edited = client.patch(f"/api/plans/{created['id']}/actions/{action['id']}", json={"proposed_name": "my-notes.txt"})
    assert edited.json()["actions"][0]["name_source"] == "user"
    assert approve(client, created, action).status_code == 200 and execute(client, created, action).status_code == 200
    assert (workspace / "documents" / "my-notes.txt").exists()


# ---- editing a legacy plan upgrades its category but not its trust --------------------------------

def test_editing_a_legacy_plan_rederives_the_category_and_keeps_it_legacy(workspace, client):
    source, created, action = make(workspace, client)
    rewrite_plan(created["id"], keep_fingerprint=False)
    edited = client.patch(f"/api/plans/{created['id']}/actions/{action['id']}", json={"proposed_name": "renamed.txt"})
    assert edited.status_code == 200
    updated = edited.json()["actions"][0]
    assert updated["category"] == "documents" and updated["plan_schema"] == 0
    assert Path(updated["proposed_destination"]) == workspace.resolve() / "documents" / "renamed.txt"
    assert approve(client, created, updated).status_code == 409  # still refused: no fingerprint to verify against


def test_new_plans_are_stamped_with_the_current_schema_and_a_category(workspace, client):
    source, created, action = make(workspace, client)
    assert action["plan_schema"] == main.PLAN_SCHEMA_VERSION and action["category"] == "documents"

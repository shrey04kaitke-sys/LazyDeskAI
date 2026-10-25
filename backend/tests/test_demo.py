"""The synthetic demo: its setup/reset safety guards and an end-to-end review walkthrough that never applies anything."""
import hashlib
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main

DEMO_SCRIPT = Path(__file__).resolve().parents[2] / "demo" / "demo_workspace.py"
spec = importlib.util.spec_from_file_location("demo_workspace", DEMO_SCRIPT)
demo = importlib.util.module_from_spec(spec)
sys.modules["demo_workspace"] = demo
spec.loader.exec_module(demo)


@pytest.fixture
def base(tmp_path: Path) -> Path:
    return tmp_path / demo.DEMO_DIR_NAME


def snapshot(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob("*")) if p.is_file()}


# ---- setup / reset / remove -------------------------------------------------------

def test_setup_builds_a_marked_synthetic_workspace_with_nesting(base: Path):
    summary = demo.build(base)
    workspace = base / "workspace"
    files = snapshot(workspace)
    assert summary["files"] == len(files) == 22
    assert (base / demo.MARKER_NAME).read_text(encoding="utf-8") == demo.MARKER_TEXT
    assert max(len(Path(name).parts) for name in files) == 4  # Projects/Alpha/assets/logo.png
    assert all((workspace / name).stat().st_size < 1024 for name in files)  # tiny generated placeholders only


def test_setup_is_deterministic_and_rebuilds_after_changes(base: Path):
    demo.build(base)
    original = snapshot(base / "workspace")
    (base / "workspace" / "Inbox" / "budget.csv").unlink()
    (base / "workspace" / "extra.txt").write_text("user clutter", encoding="utf-8")
    (base / "demo.sqlite").write_bytes(b"old database")
    demo.build(base)  # `reset` is the same operation
    assert snapshot(base / "workspace") == original
    assert not (base / "demo.sqlite").exists()
    assert demo.main(["reset", "--base", str(base)]) == 0
    assert snapshot(base / "workspace") == original


def test_remove_deletes_only_demo_data_and_keeps_foreign_files(base: Path):
    demo.build(base)
    (base / "demo.sqlite").write_bytes(b"db")
    (base / "demo.sqlite-wal").write_bytes(b"wal")
    (base / "keep-me.txt").write_text("not ours", encoding="utf-8")
    result = demo.remove(base)
    assert result == {"removed": True, "folder_removed": False}
    assert [p.name for p in base.iterdir()] == ["keep-me.txt"]
    (base / "keep-me.txt").unlink()
    demo.build(base)
    assert demo.remove(base)["folder_removed"] is True and not base.exists()
    assert demo.remove(base) == {"removed": False, "reason": "nothing to remove"}


# ---- safety guards ------------------------------------------------------------------

def test_refuses_folders_not_named_lazydeskdemo(tmp_path: Path):
    victim = tmp_path / "Photos"
    victim.mkdir()
    (victim / "precious.jpg").write_bytes(b"x")
    for command in (demo.build, demo.remove, demo.info):
        with pytest.raises(demo.DemoSafetyError):
            command(victim)
    assert (victim / "precious.jpg").exists()


def test_refuses_a_nonempty_unmarked_folder_even_if_correctly_named(base: Path):
    base.mkdir()
    (base / "important.docx").write_bytes(b"x")
    for command in (demo.build, demo.remove):
        with pytest.raises(demo.DemoSafetyError):
            command(base)
    assert [p.name for p in base.iterdir()] == ["important.docx"]
    (base / "important.docx").unlink()
    demo.build(base)  # an EMPTY folder of the right name is fine


@pytest.mark.parametrize("parent", ["OneDrive", "Desktop", "Downloads", "Documents", "onedrive"])
def test_refuses_personal_and_cloud_locations(tmp_path: Path, parent: str):
    target = tmp_path / parent / demo.DEMO_DIR_NAME
    with pytest.raises(demo.DemoSafetyError):
        demo.build(target)
    assert not (tmp_path / parent).exists()


def test_refuses_drive_roots_and_the_home_folder(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    with pytest.raises(demo.DemoSafetyError):
        demo.validate_base(Path(tmp_path.anchor))
    target = tmp_path / demo.DEMO_DIR_NAME
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: target))  # home folder is the demo folder
    with pytest.raises(demo.DemoSafetyError):
        demo.validate_base(target)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: target / "user"))  # demo folder contains home
    with pytest.raises(demo.DemoSafetyError):
        demo.validate_base(target)
    assert not target.exists()


def test_refuses_a_linked_demo_folder_and_never_follows_a_linked_workspace(tmp_path: Path):
    real = tmp_path / "real"
    real.mkdir()
    (real / "personal.txt").write_text("keep", encoding="utf-8")
    link_base = tmp_path / demo.DEMO_DIR_NAME
    try:
        link_base.symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this system")
    with pytest.raises(demo.DemoSafetyError):
        demo.build(link_base)
    assert (real / "personal.txt").exists() and not (real / "workspace").exists()


def test_refuses_to_delete_a_linked_workspace(base: Path, tmp_path: Path):
    demo.build(base)
    real = tmp_path / "elsewhere"
    real.mkdir()
    (real / "personal.txt").write_text("keep", encoding="utf-8")
    import shutil
    shutil.rmtree(base / "workspace")
    try:
        (base / "workspace").symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this system")
    with pytest.raises(demo.DemoSafetyError):
        demo.build(base)
    assert (real / "personal.txt").read_text(encoding="utf-8") == "keep"


def test_command_line_exit_codes_and_default_location(tmp_path: Path):
    wrong = tmp_path / "NotTheDemo"
    wrong.mkdir()
    refused = subprocess.run([sys.executable, "-I", str(DEMO_SCRIPT), "reset", "--base", str(wrong)], capture_output=True, text=True)
    assert refused.returncode == 2 and "SAFETY STOP" in refused.stderr
    ok = subprocess.run([sys.executable, "-I", str(DEMO_SCRIPT), "info", "--base", str(tmp_path / demo.DEMO_DIR_NAME)], capture_output=True, text=True)
    assert ok.returncode == 0 and demo.DEMO_DIR_NAME in ok.stdout
    assert demo.default_base().name == demo.DEMO_DIR_NAME


def test_demo_database_is_separate_from_the_application_database(base: Path):
    database = Path(demo.info(base)["database"])
    real = Path(main.APP_ROOT) / "lazydesk.db"
    assert database != real and real.parent not in database.parents
    assert main.DB_PATH != real  # the test suite itself runs on a temporary database


# ---- end-to-end walkthrough (scan -> duplicates -> plan -> review), nothing approved or applied ------

def test_demo_walkthrough_scan_duplicates_and_review_without_applying(base: Path):
    demo.build(base)
    workspace = base / "workspace"
    before = snapshot(workspace)
    client = TestClient(main.app)

    scan = client.post("/api/scan", json={"path": str(workspace)}).json()
    assert scan["count"] == 22 and scan["completeness"]["complete"] is True
    categories = {item["relative_path"]: item["category"] for item in scan["items"]}
    assert categories["Inbox/beach-photo.jpg"] == "images" and categories["Inbox/voice-memo.mp3"] == "audio"
    assert categories["Archive/2023/mystery.xyz"] == "other" and categories["Archive/2023/readme"] == "other"

    duplicates = client.post("/api/duplicates", json={"path": str(workspace)}).json()
    groups = sorted(sorted(Path(name).relative_to(workspace).as_posix() for name in group["files"]) for group in duplicates["groups"])
    assert groups == [
        ["Inbox/budget.csv", "Projects/Alpha/budget-copy.csv"],
        ["Inbox/invoice_2024_03 (1).pdf", "Inbox/invoice_2024_03.pdf"],
        ["Projects/Alpha/assets/logo-copy.png", "Projects/Alpha/assets/logo.png"],
        ["Projects/Alpha/report-final-FINAL.docx", "Projects/Alpha/report.docx"],
    ]
    assert duplicates["skipped_cloud_only"] == 0

    chosen = ["Inbox/invoice_2024_03.pdf", "Projects/Alpha/assets/logo.png", "Projects/Beta/notes.txt", "Projects/Alpha/notes.txt", "Archive/2023/mystery.xyz"]
    plan = client.post("/api/plans", json={"workspace_root": str(workspace), "files": [str(workspace / name) for name in chosen]})
    assert plan.status_code == 200
    actions = plan.json()["actions"]
    assert {a["workspace_root"] for a in actions} == {str(workspace.resolve())}
    assert all(Path(a["proposed_destination"]).is_relative_to(workspace.resolve()) for a in actions)
    assert all(a["status"] == "pending" and a["validation"] == "warning" for a in actions)  # nothing is pre-approved
    destinations = {Path(a["source"]).relative_to(workspace.resolve()).as_posix(): Path(a["proposed_destination"]).relative_to(workspace.resolve()).as_posix() for a in actions}
    assert destinations["Projects/Alpha/assets/logo.png"] == "Projects/Alpha/assets/images/logo.png"
    assert destinations["Projects/Beta/notes.txt"] == "Projects/Beta/documents/notes.txt"  # same name as Alpha/notes.txt, no clash
    assert destinations["Archive/2023/mystery.xyz"] == "Archive/2023/other/mystery.xyz"

    unapproved = client.post("/api/execute", json={"plan_id": plan.json()["id"], "action_ids": [actions[0]["id"]]})
    assert unapproved.status_code == 409
    assert snapshot(workspace) == before  # the demo files are exactly as generated

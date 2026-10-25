"""Phase 2: protected workspace folders. No override exists; the policy runs wherever a workspace is accepted and again
when a plan is approved or applied. Pure rule tests run on every host (Windows rules are evaluated on Linux CI too);
real-filesystem tests use the fake user profile from conftest, real system folders, and synthetic temp folders only."""
import dataclasses
import json
import os
import re
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main, workspace_policy as wp
from apply_helpers import apply_moves  # noqa: E402

WINDOWS_ONLY = pytest.mark.skipif(os.name != "nt", reason="Windows-only behaviour")
POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="POSIX-only behaviour")

WIN_ENV = wp.build_environment(True, {
    "SystemRoot": r"C:\Windows", "windir": r"C:\WINDOWS", "SystemDrive": "C:", "ProgramFiles": r"C:\Program Files",
    "ProgramFiles(x86)": r"C:\Program Files (x86)", "ProgramW6432": r"C:\Program Files", "ProgramData": r"C:\ProgramData",
    "ALLUSERSPROFILE": r"C:\ProgramData", "PUBLIC": r"C:\Users\Public", "USERPROFILE": r"C:\Users\me",
    "APPDATA": r"C:\Users\me\AppData\Roaming", "LOCALAPPDATA": r"C:\Users\me\AppData\Local",
})
POSIX_ENV = wp.build_environment(False, {"HOME": "/home/me"})


@pytest.fixture
def client() -> TestClient:
    return TestClient(main.app)


def make(path: Path, *names: str) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    for name in names:
        (path / name).write_text("synthetic", encoding="utf-8")
    return path


def refusal(raw: str, env=WIN_ENV) -> str | None:
    """Code the pure rules give for `raw` (text only: no filesystem access), or None when it would be accepted."""
    try:
        return wp.classify(wp.normalize_raw(raw, env.windows), env)
    except wp.WorkspaceRefused as error:
        return error.code


# ======================================================================================================================
# 1. Pure rules, Windows flavour (run on every host)
# ======================================================================================================================

@pytest.mark.parametrize("raw,code", [
    ("C:\\", "drive_root"), ("c:/", "drive_root"), ("D:\\", "drive_root"), ("Z:", None) if False else ("C:\\Windows\\..", "drive_root"),
    ("C:\\Windows", "system_folder"), ("c:\\WINDOWS", "system_folder"), ("C:\\Windows\\", "system_folder"),
    ("C:\\Windows\\System32", "system_folder"), ("c:/windows/system32/drivers", "system_folder"),
    ("C:\\Windows.", "system_folder"), ("C:\\Windows ", "system_folder"), ("C:\\Windows...\\System32", "system_folder"),
    ("C:\\Windows\\..\\Windows\\System32", "system_folder"), ("C:\\Users\\me\\..\\..\\Windows", "system_folder"),
    ("C:\\Windows.old\\Users", "system_folder"),
    ("C:\\Program Files", "program_folder"), ("C:\\Program Files\\Some App\\data", "program_folder"),
    ("c:\\program files (x86)", "program_folder"), ("C:\\Program Files (x86)\\X", "program_folder"),
    ("C:\\ProgramData", "program_data"), ("C:\\ProgramData\\Microsoft\\Windows", "program_data"),
    ("C:\\Users", "users_folder"), ("c:\\USERS\\", "users_folder"), ("C:\\Users\\.", "users_folder"),
    ("C:\\Users\\Public", "other_profile"), ("C:\\Users\\Public\\Documents", "other_profile"),
    ("C:\\Users\\someoneElse", "other_profile"), ("C:\\Users\\someoneElse\\Documents\\x", "other_profile"),
    ("c:\\users\\SOMEONEELSE", "other_profile"), ("C:\\Users\\Default", "other_profile"), ("C:\\Users\\All Users", "other_profile"),
    ("C:\\Users\\me2", "other_profile"), ("C:\\Users\\meX\\Documents", "other_profile"),   # prefix of "me" is NOT "me"
    ("C:\\Users\\me", "profile_root"), ("c:\\users\\ME\\", "profile_root"), ("C:\\Users\\me\\Documents\\..", "profile_root"),
    ("C:\\Users\\me\\AppData", "app_data"), ("C:\\Users\\me\\appdata\\Local", "app_data"),
    ("C:\\Users\\me\\AppData\\Local\\Temp\\x", "app_data"), ("C:\\Users\\me\\AppData\\Roaming\\App", "app_data"),
    ("C:\\Users\\me\\.ssh", "credentials"), ("C:\\Users\\me\\.SSH", "credentials"), ("C:\\Users\\me\\.aws\\x", "credentials"),
    ("C:\\Users\\me\\Documents\\.gnupg", "credentials"), ("D:\\backup\\old\\.azure", "credentials"), ("E:\\.kube\\cache", "credentials"),
    ("C:\\Users\\me\\Documents\\..\\.ssh", "credentials"), ("C:\\Users\\me\\.ssh. ", "credentials"),
    ("D:\\$Recycle.Bin", "system_volume"), ("D:\\$recycle.bin\\S-1-5-21", "system_volume"),
    ("D:\\System Volume Information", "system_volume"), ("D:\\data\\System Volume Information\\x", "system_volume"),
    # supported and unsupported prefixes, decided from the text alone
    ("\\\\?\\C:\\Windows", "system_folder"), ("\\\\?\\c:\\WINDOWS\\System32", "system_folder"), ("//?/C:/Windows", "system_folder"),
    ("\\\\?\\C:\\Users", "users_folder"), ("\\\\?\\C:\\", "drive_root"),
    ("\\\\?\\UNC\\server\\share\\x", "unc"), ("\\\\?\\unc\\server\\share", "unc"), ("\\\\server\\share", "unc"),
    ("\\\\server\\share\\folder", "unc"), ("//server/share/folder", "unc"), ("\\\\.\\UNC\\server\\share", "unc"),
    ("\\\\?\\Volume{01234567-89ab-cdef-0123-456789abcdef}\\Windows", "unsupported_prefix"), ("\\\\?\\GLOBALROOT\\Device\\X", "unsupported_prefix"),
    ("\\\\.\\C:\\Windows", "device_path"), ("\\\\.\\PhysicalDrive0", "device_path"), ("\\??\\C:\\Windows", "device_path"),
    ("", "invalid_path"), ("   ", "invalid_path"), ("C:\\bad\x00name", "invalid_path"),
])
def test_windows_rules_refuse_these_locations(raw, code):
    assert refusal(raw) == code


@pytest.mark.parametrize("raw", [
    "C:\\Users\\me\\Documents", "C:\\Users\\me\\Downloads\\project", "c:\\users\\ME\\Desktop", "C:\\Users\\me\\OneDrive\\Desktop\\00_Home",
    "C:\\Users\\me\\Documents\\.git\\..", "C:\\Users\\me\\DocumentsX", "C:\\Users\\me\\AppDataX\\stuff", "C:\\Users\\me\\.sshkeys",
    "D:\\Projects\\app", "E:\\Photos 2024", "C:\\Data\\Windows", "C:\\Work\\Program Files",
    # prefix / look-alike traps: none of these IS a protected folder
    "C:\\WindowsX", "C:\\Windows2\\System32", "C:\\Program FilesX", "C:\\Program Files (x86)X", "C:\\ProgramDataX",
    "C:\\UsersX\\me", "C:\\Users\\me\\ProgramData", "D:\\Windows", "D:\\Program Files",
    "\\\\?\\C:\\Users\\me\\Documents", "//?/D:/Projects/app", "C:/Users/me/Documents/../Downloads",
])
def test_windows_rules_allow_ordinary_folders_and_prefix_lookalikes(raw):
    assert refusal(raw) is None


def test_windows_rules_never_consult_the_filesystem_for_unc_or_device_paths(monkeypatch):
    def explode(*_a, **_k):
        raise AssertionError("the filesystem or network was touched")
    with monkeypatch.context() as patched:  # undone before fixture teardown, which needs the real filesystem
        patched.setattr(Path, "resolve", explode)
        patched.setattr(os, "stat", explode)
        patched.setattr(os, "scandir", explode)
        for raw in ("\\\\server\\share\\x", "\\\\?\\UNC\\server\\share", "//server/share", "\\\\.\\C:\\x", "\\\\?\\GLOBALROOT\\x"):
            with pytest.raises(wp.WorkspaceRefused):
                wp.check_workspace(raw, app_db=None, env=WIN_ENV)


def test_a_profile_directly_under_a_drive_root_does_not_make_everything_another_users_profile():
    env = wp.build_environment(True, {"USERPROFILE": "C:\\me", "SystemDrive": "C:", "PUBLIC": "C:\\Users\\Public"})
    assert wp.classify("C:\\me\\Documents", env) is None
    assert wp.classify("D:\\Projects", env) is None
    assert wp.classify("C:\\me", env) == "profile_root"
    assert wp.classify("C:\\Users\\Other", env) == "other_profile"


def test_without_a_known_profile_every_profile_folder_is_refused_not_allowed():
    env = wp.build_environment(True, {"SystemDrive": "C:"})
    assert wp.classify("C:\\Users\\anyone\\Documents", env) == "other_profile"  # fail closed


# ======================================================================================================================
# 2. Pure rules, POSIX flavour (run on every host)
# ======================================================================================================================

@pytest.mark.parametrize("raw,code", [
    ("/", "drive_root"), ("/etc", "system_folder"), ("/etc/ssh", "system_folder"), ("/usr", "system_folder"), ("/usr/local/bin", "system_folder"),
    ("/bin", "system_folder"), ("/sbin", "system_folder"), ("/boot", "system_folder"), ("/dev", "system_folder"), ("/proc/1", "system_folder"),
    ("/sys", "system_folder"), ("/root", "system_folder"), ("/lib64", "system_folder"), ("/var/lib/docker", "system_folder"),
    ("/System/Library", "system_folder"), ("/Applications", "system_folder"), ("/opt/tool", "system_folder"),
    ("/home", "users_folder"), ("/Users", "users_folder"), ("/home/other", "other_profile"), ("/home/other/Documents", "other_profile"),
    ("/home/me2", "other_profile"), ("/home/me", "profile_root"), ("/home/me/proj/../..", "users_folder"),
    ("/home/me/.ssh", "credentials"), ("/home/me/proj/.aws/x", "credentials"), ("/mnt/backup/.gnupg", "credentials"),
    ("/mnt", "drive_root"), ("/media", "drive_root"), ("/Volumes", "drive_root"),
])
def test_posix_rules_refuse_these_locations(raw, code):
    assert refusal(raw, POSIX_ENV) == code


@pytest.mark.parametrize("raw", [
    "/home/me/Documents", "/home/me/proj/app", "/tmp/work", "/tmp", "/mnt/data/stuff", "/media/usb/photos", "/srv/files",
    "/etcetera", "/usrlocal", "/homeX/me", "/home/me/.sshkeys", "/home/me/etc", "/var/tmp/x", "/ETC",
])
def test_posix_rules_allow_ordinary_folders_and_prefix_lookalikes(raw):
    assert refusal(raw, POSIX_ENV) is None


def test_a_home_directly_under_the_root_does_not_make_everything_another_users_home():
    env = wp.build_environment(False, {"HOME": "/root"})
    assert wp.classify("/tmp/x", env) is None and wp.classify("/srv/data", env) is None


# ======================================================================================================================
# 3. Nested protected folders: skipped, never traversed, and reported
# ======================================================================================================================

def protected_tree(base: Path) -> Path:
    root = base / "workspace"
    make(root, "top.txt")
    make(root / "ok", "a.txt")
    make(root / ".ssh", "id_rsa.txt")
    make(root / "docs" / ".aws", "credentials.txt")
    make(root / "docs", "readme.md")
    if os.name == "nt":  # the recycle-bin and system-volume rules are Windows rules
        make(root / "$Recycle.Bin", "gone.txt")
        make(root / "System Volume Information", "data.txt")
    make(root / (".GnuPG" if os.name == "nt" else ".gnupg"), "key.txt")  # names are case-insensitive on Windows only
    return root


def test_scan_skips_protected_subfolders_reports_them_and_never_reads_inside(tmp_path, client, monkeypatch):
    root = protected_tree(tmp_path)
    opened = []
    real = main.content_fingerprint
    monkeypatch.setattr(main, "content_fingerprint", lambda path: opened.append(str(path)) or real(path))
    body = client.post("/api/scan", json={"path": str(root)}).json()
    names = sorted(item["relative_path"] for item in body["items"])
    assert names == ["docs/readme.md", "ok/a.txt", "top.txt"]
    summary = body["completeness"]
    skipped = {item["path"]: item["reason"] for item in summary["skipped_protected"]}
    on_windows = os.name == "nt"
    expected = {".ssh": "credentials folder", "docs/.aws": "credentials folder", (".GnuPG" if os.name == "nt" else ".gnupg"): "credentials folder"}
    if on_windows:  # the recycle bin and system-volume rules are Windows rules
        expected.update({"$Recycle.Bin": "protected system data", "System Volume Information": "protected system data"})
    assert {k: v for k, v in skipped.items() if k in expected} == expected
    assert summary["skipped_protected_total"] == len(summary["skipped_protected"]) >= 3
    assert summary["complete"] is True and summary["truncated"] is False  # "complete" is about the file cap; omissions are listed separately
    assert not any(".ssh" in p or ".aws" in p or "nupg" in p for p in opened)


def test_duplicates_and_benchmarks_skip_and_report_the_same_way(tmp_path, client):
    root = protected_tree(tmp_path)
    (root / ".ssh" / "copy.txt").write_text("synthetic", encoding="utf-8")  # a duplicate that must never be found
    duplicates = client.post("/api/duplicates", json={"path": str(root)}).json()
    assert duplicates["count"] >= 0 and all(".ssh" not in name for group in duplicates["groups"] for name in group["files"])
    assert any(item["path"] == ".ssh" for item in duplicates["completeness"]["skipped_protected"])
    benchmark = client.post("/api/benchmarks", json={"path": str(root), "repetitions": 1}).json()
    assert benchmark["files_discovered"] == 3  # top.txt, ok/a.txt, docs/readme.md


def test_the_report_is_capped_but_the_total_is_always_exact(tmp_path, client, monkeypatch):
    root = tmp_path / "ws"
    for index in range(7):
        make(root / f"p{index}" / ".ssh", "k.txt")
    make(root, "keep.txt")
    monkeypatch.setattr(main, "SKIP_REPORT_LIMIT", 3)
    summary = client.post("/api/scan", json={"path": str(root)}).json()["completeness"]
    assert len(summary["skipped_protected"]) == 3 and summary["skipped_protected_total"] == 7


def test_a_nested_folder_with_a_protected_name_is_skipped_even_when_uppercase_on_windows(tmp_path, client):
    root = make(tmp_path / "ws", "keep.txt")
    make(root / ".SSH", "k.txt")
    names = [i["relative_path"] for i in client.post("/api/scan", json={"path": str(root)}).json()["items"]]
    assert ("k.txt" in " ".join(names)) == (os.name != "nt")  # case-insensitive on Windows, case-sensitive on POSIX


def test_files_in_a_protected_subfolder_cannot_be_planned(tmp_path, client):
    root = protected_tree(tmp_path)
    response = client.post("/api/plans", json={"workspace_root": str(root), "files": [str(root / ".ssh" / "id_rsa.txt")]})
    assert response.status_code == 403 and response.json()["detail"]["code"] == "credentials"
    assert (root / ".ssh" / "id_rsa.txt").exists()
    deeper = client.post("/api/plans", json={"workspace_root": str(root), "files": [str(root / "docs" / ".aws" / "credentials.txt")]})
    assert deeper.status_code == 403
    assert client.post("/api/plans", json={"workspace_root": str(root), "files": [str(root / "ok" / "a.txt")]}).status_code == 200


# ======================================================================================================================
# 4. Real folders through the API (fake profile from conftest + real system folders)
# ======================================================================================================================

ENDPOINTS = [
    ("/api/scan", lambda root: {"path": str(root)}),
    ("/api/duplicates", lambda root: {"path": str(root)}),
    ("/api/benchmarks", lambda root: {"path": str(root)}),
    ("/api/plans", lambda root: {"workspace_root": str(root), "files": [str(root / "x.txt")]}),
]


@pytest.mark.parametrize("endpoint,payload", ENDPOINTS, ids=[e for e, _ in ENDPOINTS])
def test_every_endpoint_that_accepts_a_workspace_applies_the_policy(client, fake_profile, endpoint, payload):
    other = make(fake_profile.users / "alice" / "Documents", "private.txt")
    for root in (fake_profile.profile, fake_profile.users, other, fake_profile.public, Path(os.path.abspath(os.sep))):
        response = client.post(endpoint, json=payload(root))
        assert response.status_code == 403, (endpoint, root, response.text)
        detail = response.json()["detail"]
        assert detail["code"] and workspace_policy_advice() in detail["message"]
    assert (other / "private.txt").exists()


def workspace_policy_advice() -> str:
    return wp.ADVICE


def test_ordinary_user_folders_are_accepted(client, fake_profile, tmp_path):
    for folder in (fake_profile.profile / "Documents", fake_profile.profile / "Downloads", fake_profile.profile / "Desktop",
                   fake_profile.profile / "Downloads" / "my project", fake_profile.profile / "OneDrive" / "Desktop" / "00_Home", tmp_path / "scratch"):
        make(folder, "a.txt")
        response = client.post("/api/scan", json={"path": str(folder)})
        assert response.status_code == 200 and response.json()["count"] == 1, (folder, response.text)


def test_current_profile_lookalikes_are_not_mistaken_for_the_current_profile(client, fake_profile):
    make(fake_profile.users / "tester2" / "Documents")          # starts with the current profile's name
    make(fake_profile.users / "testerX")
    for folder in (fake_profile.users / "tester2", fake_profile.users / "tester2" / "Documents", fake_profile.users / "testerX"):
        assert client.post("/api/scan", json={"path": str(folder)}).status_code == 403
    make(fake_profile.profile / "DocumentsX", "a.txt")
    assert client.post("/api/scan", json={"path": str(fake_profile.profile / "DocumentsX")}).status_code == 200


@pytest.mark.parametrize("name", [".ssh", ".aws", ".gnupg"])
def test_credential_folders_are_refused_as_workspaces_wherever_they_are(client, fake_profile, tmp_path, name):
    for folder in (fake_profile.profile / name, fake_profile.profile / "Documents" / name, tmp_path / name, tmp_path / "x" / name / "deep"):
        make(folder, "k.txt")
        response = client.post("/api/scan", json={"path": str(folder)})
        assert response.status_code == 403 and response.json()["detail"]["code"] == "credentials"


@WINDOWS_ONLY
def test_appdata_is_refused_including_a_lowercase_spelling(client, fake_profile):
    make(fake_profile.profile / "AppData" / "Local" / "SomeApp", "x.txt")
    for raw in (fake_profile.profile / "AppData", fake_profile.profile / "AppData" / "Local", fake_profile.profile / "AppData" / "Local" / "SomeApp"):
        assert client.post("/api/scan", json={"path": str(raw)}).status_code == 403
    assert client.post("/api/scan", json={"path": str(fake_profile.profile / "AppData" / "Local" / "SomeApp").lower()}).status_code == 403


@WINDOWS_ONLY
@pytest.mark.parametrize("variable", ["SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramData"])
def test_real_windows_system_folders_are_refused(client, variable):
    folder = os.environ.get(variable)
    if not folder or not Path(folder).exists():
        pytest.skip(f"{variable} is not present on this machine")
    for raw in (folder, folder.lower(), folder.upper(), folder + "\\", "\\\\?\\" + folder, folder + ".", folder.replace("\\", "/")):
        assert client.post("/api/scan", json={"path": raw}).status_code == 403, raw
    inner = next((p for p in Path(folder).iterdir() if p.is_dir()), None)
    if inner:
        assert client.post("/api/scan", json={"path": str(inner)}).status_code == 403


@WINDOWS_ONLY
def test_windows_aliases_of_protected_folders_resolve_to_the_real_location(client):
    windows = Path(os.environ["SystemRoot"])
    for raw in (str(windows / "System32" / ".." / ".."), str(windows / ".." / "Windows"), "C:\\PROGRA~1"):
        if raw.endswith("PROGRA~1") and not Path(raw).exists():
            continue  # 8.3 short names are disabled on this volume
        assert client.post("/api/scan", json={"path": raw}).status_code == 403, raw
    assert client.post("/api/scan", json={"path": "\\\\.\\" + str(windows)}).status_code == 403


@WINDOWS_ONLY
def test_a_windows_drive_root_is_refused_in_every_spelling(client):
    drive = os.path.splitdrive(os.environ["SystemRoot"])[0]
    for raw in (drive + "\\", drive + "/", drive.lower() + "\\", "\\\\?\\" + drive + "\\", str(Path(os.environ["SystemRoot"]) / ".." )):
        assert client.post("/api/scan", json={"path": raw}).status_code == 403, raw


@WINDOWS_ONLY
def test_unc_paths_are_refused_without_touching_the_network(client, monkeypatch):
    def explode(*_a, **_k):
        raise AssertionError("the filesystem was touched")
    with monkeypatch.context() as patched:
        patched.setattr(Path, "resolve", explode)
        for raw in ("\\\\server\\share", "\\\\server\\share\\sub", "//server/share/x", "\\\\?\\UNC\\server\\share"):
            response = client.post("/api/scan", json={"path": raw})
            assert response.status_code == 403 and response.json()["detail"]["code"] == "unc", raw


@POSIX_ONLY
@pytest.mark.parametrize("folder", ["/", "/etc", "/usr", "/bin", "/proc", "/sys"])
def test_real_posix_system_folders_are_refused(client, folder):
    if not Path(folder).exists():
        pytest.skip(f"{folder} does not exist")
    assert client.post("/api/scan", json={"path": folder}).status_code == 403
    assert client.post("/api/scan", json={"path": folder + "//"}).status_code == 403


# ---- links: symlinks everywhere, junctions on Windows ------------------------------------------------------------------------------

def link_dir(link: Path, target: Path) -> str:
    """Create a directory link and return its kind, or skip the test when the platform/user cannot."""
    if os.name == "nt":
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, text=True)
        if result.returncode:
            pytest.skip("cannot create a junction here: " + result.stderr.strip())
        return "junction"
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    return "symlink"


def remove_link(link: Path) -> None:
    """Remove ONLY the link. Recursive cleanup must never be allowed to follow a junction into its target."""
    if os.path.lexists(link):
        if link.is_junction():
            os.rmdir(link)
        else:
            link.unlink()
    assert not os.path.lexists(link)


def test_a_workspace_that_is_a_link_to_a_protected_folder_is_refused_by_where_it_really_points(client, fake_profile, tmp_path):
    target = make(fake_profile.profile / ".ssh", "id.txt")
    link = tmp_path / "innocent-name"
    link_dir(link, target)
    try:
        response = client.post("/api/scan", json={"path": str(link)})
        assert response.status_code == 403 and response.json()["detail"]["code"] == "credentials"
        other = make(fake_profile.users / "alice" / "Documents")
        remove_link(link)
        link_dir(link, other)
        assert client.post("/api/scan", json={"path": str(link)}).status_code == 403
    finally:
        remove_link(link)
    assert (target / "id.txt").exists()


@WINDOWS_ONLY
def test_a_junction_to_the_real_windows_folder_is_refused(client, tmp_path):
    link = tmp_path / "looks-harmless"
    link_dir(link, Path(os.environ["SystemRoot"]))
    try:
        for suffix in ("", "\\System32"):
            response = client.post("/api/scan", json={"path": str(link) + suffix})
            assert response.status_code == 403 and response.json()["detail"]["code"] == "system_folder"
    finally:
        remove_link(link)
    assert (Path(os.environ["SystemRoot"]) / "System32").exists()  # the target was never touched


def test_a_link_to_an_ordinary_folder_resolves_to_its_real_location_and_is_accepted(client, tmp_path):
    real = make(tmp_path / "real", "a.txt")
    link = tmp_path / "alias"
    link_dir(link, real)
    try:
        body = client.post("/api/scan", json={"path": str(link)}).json()
        assert body["count"] == 1 and body["path"] == str(real.resolve())
    finally:
        remove_link(link)


def test_links_inside_a_workspace_are_not_followed_and_are_counted(client, tmp_path):
    root = make(tmp_path / "ws", "keep.txt")
    secret = make(tmp_path / "outside", "secret.txt")
    link = root / "shortcut"
    link_dir(link, secret)
    try:
        body = client.post("/api/scan", json={"path": str(root)}).json()
        assert [i["relative_path"] for i in body["items"]] == ["keep.txt"]
        assert body["completeness"]["skipped_links"] == 1
    finally:
        remove_link(link)
    assert (secret / "secret.txt").exists()


@WINDOWS_ONLY
def test_a_junction_inside_a_workspace_that_points_at_a_protected_folder_is_skipped(client, tmp_path):
    root = make(tmp_path / "ws", "keep.txt")
    link = root / "system"
    link_dir(link, Path(os.environ["SystemRoot"]))
    try:
        body = client.post("/api/scan", json={"path": str(root)}).json()
        assert [i["relative_path"] for i in body["items"]] == ["keep.txt"] and body["completeness"]["skipped_links"] == 1
    finally:
        remove_link(link)
    assert (Path(os.environ["SystemRoot"]) / "System32").exists()


# ---- missing and inaccessible --------------------------------------------------------------------------------------------------------------------

def test_missing_paths_keep_their_404_and_a_file_is_not_a_directory(client, tmp_path):
    assert client.post("/api/scan", json={"path": str(tmp_path / "nope")}).status_code == 404
    (tmp_path / "f.txt").write_text("x", encoding="utf-8")
    assert client.post("/api/scan", json={"path": str(tmp_path / "f.txt")}).status_code == 400
    assert client.post("/api/scan", json={"path": str(tmp_path / "f.txt" / "child")}).status_code == 404


def test_an_inaccessible_folder_gets_a_clear_non_sensitive_refusal(client, tmp_path, monkeypatch):
    folder = make(tmp_path / "locked")
    real = Path.resolve

    def deny(self, *args, **kwargs):
        if self.name == "locked":
            raise PermissionError("denied")
        return real(self, *args, **kwargs)
    monkeypatch.setattr(Path, "resolve", deny)
    response = client.post("/api/scan", json={"path": str(folder)})
    assert response.status_code == 403 and response.json()["detail"]["code"] == "inaccessible"


def test_paths_with_nul_characters_are_refused(client):
    response = client.post("/api/scan", json={"path": "C:\\x\x00y" if os.name == "nt" else "/tmp/x\x00y"})
    assert response.status_code == 403 and response.json()["detail"]["code"] == "invalid_path"


# ---- errors: informative but not revealing ----------------------------------------------------------------------------------------------------------

def test_a_refusal_explains_why_suggests_a_safe_folder_and_reveals_nothing_about_other_users(client, fake_profile):
    alice = make(fake_profile.users / "alice" / "Documents", "diary.txt")
    make(fake_profile.users / "bob" / "Secrets", "plan.txt")
    body = client.post("/api/scan", json={"path": str(alice)}).json()
    text = json.dumps(body)
    assert body["detail"]["code"] == "other_profile" and "another user profile" in body["detail"]["message"]
    assert "Documents, Downloads or Desktop" in body["detail"]["message"]
    for leak in ("bob", "Secrets", "diary", "plan.txt"):
        assert leak not in text


def test_a_protected_path_gets_the_same_answer_whether_or_not_it_exists(client, fake_profile):
    make(fake_profile.users / "alice" / "Documents")
    existing = client.post("/api/scan", json={"path": str(fake_profile.users / "alice" / "Documents")})
    ghost = client.post("/api/scan", json={"path": str(fake_profile.users / "ghost" / "Documents")})
    assert (existing.status_code, existing.json()["detail"]["code"]) == (ghost.status_code, ghost.json()["detail"]["code"]) == (403, "other_profile")
    assert client.post("/api/scan", json={"path": str(fake_profile.profile / ".ssh" / "does-not-exist")}).status_code == 403


# ---- the application's own database ------------------------------------------------------------------------------------------------------------------

def test_a_workspace_that_contains_the_database_is_refused_without_naming_it(client, tmp_path, monkeypatch):
    root = make(tmp_path / "ws", "a.txt")
    for database in (root / "lazydesk.db", root / "deep" / "er" / "state.sqlite"):
        monkeypatch.setattr(main, "DB_PATH", database)
        for target in (root, tmp_path):
            response = client.post("/api/scan", json={"path": str(target)})
            assert response.status_code == 403 and response.json()["detail"]["code"] == "app_database"
            assert database.name not in response.text and "contains LazyDesk's own database" in response.text


def test_workspaces_beside_or_below_the_database_are_fine(client, tmp_path, monkeypatch):
    state = make(tmp_path / "state")
    monkeypatch.setattr(main, "DB_PATH", state / "lazydesk.db")
    sibling = make(tmp_path / "sibling", "a.txt")
    child = make(state / "files", "b.txt")
    assert client.post("/api/scan", json={"path": str(sibling)}).status_code == 200
    assert client.post("/api/scan", json={"path": str(child)}).status_code == 200
    assert client.post("/api/scan", json={"path": str(state)}).status_code == 403


# ---- there is no override ----------------------------------------------------------------------------------------------------------------------------

def test_no_flag_environment_variable_or_request_field_can_relax_the_policy(client, fake_profile, monkeypatch):
    for name in ("LAZYDESK_ALLOW_PROTECTED", "LAZYDESK_UNSAFE", "LAZYDESK_FORCE", "LAZYDESK_OVERRIDE", "LAZYDESK_PROTECTED_FOLDERS", "LAZYDESK_ALLOW_ANY_FOLDER"):
        monkeypatch.setenv(name, "1")
    target = make(fake_profile.profile / ".ssh", "k.txt")
    for body in ({"path": str(target)}, {"path": str(target), "force": True, "allow_protected": True, "override": True, "unsafe": True}):
        assert client.post("/api/scan", json=body).status_code == 403
    assert client.post("/api/scan?override=1&force=true", json={"path": str(target)}).status_code == 403
    assert client.post("/api/scan", json={"path": str(target)}, headers={"X-LazyDesk-Override": "1"}).status_code == 403


def test_the_policy_reads_only_standard_operating_system_variables():
    source = (Path(wp.__file__)).read_text(encoding="utf-8")
    names = set(re.findall(r'env\.get\("([A-Za-z0-9_()]+)"\)', source)) | set(re.findall(r'os\.environ\b[^\n]*"([A-Za-z0-9_()]+)"', source))
    assert names <= {"SystemRoot", "windir", "SystemDrive", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "CommonProgramFiles",
                     "CommonProgramFiles(x86)", "CommonProgramW6432", "ProgramData", "ALLUSERSPROFILE", "USERPROFILE", "PUBLIC",
                     "APPDATA", "LOCALAPPDATA", "HOME"}, names
    assert "LAZYDESK" not in source


# ======================================================================================================================
# 5. Revalidation when a plan is approved or applied
# ======================================================================================================================

def make_plan(client, root: Path, name="a.txt") -> tuple[dict, dict, Path]:
    source = make(root, name) / name
    created = client.post("/api/plans", json={"workspace_root": str(root), "files": [str(source)]})
    assert created.status_code == 200, created.text
    plan = created.json()
    return plan, plan["actions"][0], source


def protect_everything_under(monkeypatch, folder: Path, code="credentials") -> None:
    base = wp.current_environment()
    key = wp._keys(str(folder.resolve()), base.windows)[0]
    changed = dataclasses.replace(base, inside=base.inside + ((key, code),))
    monkeypatch.setattr(wp, "current_environment", lambda: changed)


def test_a_plan_cannot_be_approved_or_applied_after_its_workspace_becomes_protected(client, fake_profile, monkeypatch):
    root = fake_profile.profile / "Documents" / "proj"
    plan, action, source = make_plan(client, root)
    assert client.post(f"/api/plans/{plan['id']}/approve", json={"action_ids": [action["id"]]}).status_code == 200
    protect_everything_under(monkeypatch, root)   # policy changed after approval
    result = apply_moves(client, plan["id"], [action["id"]])
    assert result.status_code == 409 and "no longer allowed" in result.json()["detail"]
    assert source.exists() and not (root / "documents").exists()
    again = client.post(f"/api/plans/{plan['id']}/approve", json={"action_ids": [action["id"]]})
    assert again.status_code == 409 and "no longer allowed" in again.json()["detail"]


def stored_plan(root: Path, source: Path, destination: Path, status="approved", **overrides) -> str:
    stat = source.stat()
    action = main.PlanAction(
        id="forged00000001", source=str(source), proposed_name=destination.name, current_location=str(source.parent),
        proposed_destination=str(destination), source_type="local-rule", validation="safe", reason="forged", status=status,
        workspace_root=str(root), source_size=stat.st_size, source_mtime_ns=stat.st_mtime_ns,
        source_fingerprint=main.content_fingerprint(source), category=destination.parent.name, plan_schema=main.PLAN_SCHEMA_VERSION,
    ).model_copy(update=overrides)
    connection = main.db()
    connection.execute("INSERT INTO plans VALUES (?, ?, ?)", ("forgedplan01", main.utc_now(), json.dumps([action.model_dump()])))
    connection.commit(); connection.close()
    return "forgedplan01"


@pytest.mark.parametrize("where", ["other_user_profile", "profile_root", "users_folder", "credentials_folder", "drive_root"])
def test_a_tampered_plan_pointing_at_a_protected_workspace_is_refused_everywhere(client, fake_profile, tmp_path, where):
    roots = {
        "other_user_profile": fake_profile.users / "alice" / "Documents", "profile_root": fake_profile.profile,
        "users_folder": fake_profile.users, "credentials_folder": fake_profile.profile / ".ssh", "drive_root": Path(tmp_path.anchor),
    }
    root = roots[where]
    source = make(root, "victim.txt") / "victim.txt" if where != "drive_root" else make(tmp_path / "ws", "victim.txt") / "victim.txt"
    destination = source.parent / "documents" / "victim.txt"
    plan_id = stored_plan(root, source, destination, status="approved")
    for call in (lambda: client.post(f"/api/plans/{plan_id}/approve", json={"action_ids": ["forged00000001"]}),
                 lambda: apply_moves(client, plan_id, ["forged00000001"])):
        response = call()
        assert response.status_code == 409 and "workspace" in response.json()["detail"], response.text
    assert source.exists() and not destination.exists()


def test_a_tampered_plan_whose_source_sits_in_a_protected_subfolder_is_refused(client, fake_profile):
    root = make(fake_profile.profile / "Documents" / "proj")
    source = make(root / ".ssh", "key.txt") / "key.txt"
    plan_id = stored_plan(root, source, source.parent / "documents" / "key.txt")
    response = apply_moves(client, plan_id, ["forged00000001"])
    assert response.status_code == 409 and "protected folder" in response.json()["detail"]
    assert source.exists()


def test_a_workspace_root_swapped_for_a_link_elsewhere_is_refused(client, fake_profile, tmp_path):
    real = make(fake_profile.profile / "Documents" / "real", "a.txt")
    plan, action, source = make_plan(client, real)
    link = tmp_path / "alias-of-real"
    link_dir(link, real)
    try:
        connection = main.db()
        row = connection.execute("SELECT actions FROM plans WHERE id = ?", (plan["id"],)).fetchone()
        items = json.loads(row["actions"])
        items[0]["workspace_root"] = str(link); items[0]["status"] = "approved"
        connection.execute("UPDATE plans SET actions = ? WHERE id = ?", (json.dumps(items), plan["id"])); connection.commit(); connection.close()
        result = apply_moves(client, plan["id"], [action["id"]])
        assert result.status_code == 409 and source.exists()
    finally:
        remove_link(link)


def test_a_plan_in_an_allowed_workspace_still_applies_normally(client, fake_profile):
    root = fake_profile.profile / "Documents" / "proj"
    plan, action, source = make_plan(client, root)
    assert client.post(f"/api/plans/{plan['id']}/approve", json={"action_ids": [action["id"]]}).status_code == 200
    assert apply_moves(client, plan["id"], [action["id"]]).status_code == 200
    assert (root / "documents" / "a.txt").exists() and not source.exists()


def test_legacy_plans_in_ordinary_workspaces_are_unaffected_by_the_policy(client, fake_profile):
    root = fake_profile.profile / "Documents" / "legacy"
    source = make(root, "a.txt") / "a.txt"
    plan_id = stored_plan(root, source, root / "documents" / "a.txt", plan_schema=0, category="")
    assert client.post(f"/api/plans/{plan_id}/approve", json={"action_ids": ["forged00000001"]}).status_code == 200

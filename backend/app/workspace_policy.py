"""Which folders may be used as a LazyDesk workspace.

There is deliberately NO override: not in the UI, the API, or an environment variable. The policy is applied wherever
a workspace is accepted (scan, plan creation, duplicates, benchmarks) and again when a plan is approved or applied, so
a stale or tampered plan cannot bypass it.

Design notes
- Path logic is pure and component-based (never string prefixes): `C:\\WindowsX` and `C:\\Users\\me2` are not mistaken
  for `C:\\Windows` or `C:\\Users\\me`. Both Windows and POSIX rules can be evaluated on any host, so CI on Linux still
  exercises the Windows rules.
- Windows comparisons are case-insensitive and ignore trailing dots/spaces in a component (Win32 strips them).
- A path is checked twice: lexically (before touching the filesystem, so the answer for a protected path does not depend
  on whether it exists and never reveals other users' folders) and again after resolving symlinks, junctions, `\\\\?\\`
  prefixes, 8.3 short names and mapped drives to the real location.
- UNC/device paths are refused from the text alone, before any filesystem or network call.
- Messages never include other users' folder contents or the protected path that matched.
"""
from __future__ import annotations

import ctypes
import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

ADVICE = "Choose a folder inside your own Documents, Downloads or Desktop (or another ordinary project folder) instead."

REASONS = {
    "invalid_path": "That is not a valid folder path.",
    "unc": "Network (UNC) folders are not supported as workspaces.",
    "network_drive": "Folders on network drives are not supported as workspaces.",
    "unsupported_prefix": "That path form is not supported.",
    "device_path": "Device paths are not supported as workspaces.",
    "drive_root": "A whole drive cannot be a workspace.",
    "system_folder": "That is an operating-system folder.",
    "program_folder": "That is an installed-programs folder.",
    "program_data": "That is a shared application-data folder.",
    "users_folder": "That folder holds every user's profile.",
    "other_profile": "That folder belongs to another user profile.",
    "profile_root": "Your whole user profile holds private application data and credentials.",
    "app_data": "Application data folders are not used as workspaces.",
    "credentials": "That looks like a credentials or key store.",
    "system_volume": "That is protected system-volume data.",
    "app_database": "That folder contains LazyDesk's own database.",
    "inaccessible": "That folder cannot be accessed.",
}
SHORT = {
    "system_folder": "operating-system folder", "program_folder": "programs folder", "program_data": "shared application data",
    "other_profile": "another user's profile", "app_data": "application data", "credentials": "credentials folder",
    "system_volume": "protected system data", "users_folder": "users folder", "profile_root": "profile root",
    "drive_root": "drive root", "unc": "network folder", "network_drive": "network drive", "app_database": "LazyDesk database folder",
}

CREDENTIAL_NAMES = {".ssh", ".aws", ".gnupg", ".gpg", ".azure", ".kube", ".docker", ".password-store"}
SYSTEM_VOLUME_NAMES = {"$recycle.bin", "system volume information", "$winreagent"}


class WorkspaceRefused(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        self.short = SHORT.get(code, code)
        self.message = f"{REASONS.get(code, 'That folder is not allowed.')} {ADVICE}"
        super().__init__(self.message)


class WorkspaceMissing(Exception):
    """The path does not exist (and is not protected)."""


class WorkspaceNotDirectory(Exception):
    pass


@dataclass(frozen=True)
class PolicyEnvironment:
    windows: bool
    exact: tuple[tuple[tuple[str, ...], str], ...]       # refused when equal
    inside: tuple[tuple[tuple[str, ...], str], ...]      # refused when equal or inside
    users_dirs: tuple[tuple[str, ...], ...]              # children are other users' profiles (except the current one)
    profile: tuple[str, ...] | None
    names: tuple[tuple[str, str], ...]                   # any path component with this name


# ---- pure path handling ------------------------------------------------------------------------------------

def _key(component: str, windows: bool) -> str:
    return component.rstrip(" .").casefold() if windows else component


def normalize_raw(raw: str, windows: bool) -> str:
    """Strip supported prefixes and refuse the unsupported ones, from the text alone (no filesystem or network access)."""
    if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
        raise WorkspaceRefused("invalid_path")
    if not windows:
        return raw
    text = raw.strip().replace("/", "\\")
    lowered = text.lower()
    if lowered.startswith(("\\\\?\\unc\\", "\\\\.\\unc\\")):
        raise WorkspaceRefused("unc")
    if lowered.startswith("\\\\?\\"):
        rest = text[4:]
        if re.match(r"^[A-Za-z]:(\\|$)", rest):
            return rest
        raise WorkspaceRefused("unsupported_prefix")  # \\?\Volume{...}, \\?\GLOBALROOT, ...
    if lowered.startswith(("\\\\.\\", "\\??\\")):
        raise WorkspaceRefused("device_path")
    if text.startswith("\\\\"):
        raise WorkspaceRefused("unc")
    return text


def _split(path: str, windows: bool) -> tuple[str, list[str]]:
    pure = PureWindowsPath(path) if windows else PurePosixPath(path)
    if not pure.is_absolute():
        path = os.path.abspath(path)  # relative input: resolved against the working directory of this process
        pure = PureWindowsPath(path) if windows else PurePosixPath(path)
    anchor, components = pure.parts[0], []
    for part in pure.parts[1:]:
        if part in ("", "."):
            continue
        if part == "..":
            if components:
                components.pop()
            continue
        components.append(part)
    return anchor, components


def _keys(path: str, windows: bool) -> tuple[tuple[str, ...], bool]:
    anchor, components = _split(path, windows)
    unc = windows and anchor.startswith("\\\\")
    return (_key(anchor, windows) if not windows else anchor.casefold(), *(_key(c, windows) for c in components)), unc


def _within(path_key: tuple[str, ...], base: tuple[str, ...]) -> bool:
    return len(path_key) >= len(base) and path_key[: len(base)] == base


def build_environment(windows: bool, env: dict[str, str]) -> PolicyEnvironment:
    exact: list[tuple[tuple[str, ...], str]] = []
    inside: list[tuple[tuple[str, ...], str]] = []
    users: list[tuple[str, ...]] = []

    def k(path: str | None) -> tuple[str, ...] | None:
        if not path:
            return None
        try:
            return _keys(path, windows)[0]
        except (ValueError, IndexError):
            return None

    def add(target: list, path: str | None, code: str) -> None:
        key = k(path)
        if key and (key, code) not in target:
            target.append((key, code))

    if windows:
        drive = env.get("SystemDrive") or "C:"
        for value in (env.get("SystemRoot"), env.get("windir"), f"{drive}\\Windows"):
            add(inside, value, "system_folder")
        add(inside, f"{drive}\\Windows.old", "system_folder")
        for name in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "CommonProgramFiles", "CommonProgramFiles(x86)", "CommonProgramW6432"):
            add(inside, env.get(name), "program_folder")
        for value in (f"{drive}\\Program Files", f"{drive}\\Program Files (x86)"):
            add(inside, value, "program_folder")
        for value in (env.get("ProgramData"), env.get("ALLUSERSPROFILE"), f"{drive}\\ProgramData"):
            add(inside, value, "program_data")
        profile = env.get("USERPROFILE")
        public = env.get("PUBLIC")
        for value in (profile, public):
            if value:
                users.append(k(str(PureWindowsPath(value).parent)))  # type: ignore[arg-type]
        if not users:
            users.append(k(f"{drive}\\Users"))  # type: ignore[arg-type]
        for key in users:
            if key:
                exact.append((key, "users_folder"))
        add(exact, profile, "profile_root")
        if profile:
            add(inside, str(PureWindowsPath(profile) / "AppData"), "app_data")
        for value in (env.get("APPDATA"), env.get("LOCALAPPDATA")):
            add(inside, value, "app_data")
        names = [(n, "credentials") for n in CREDENTIAL_NAMES] + [(n, "system_volume") for n in SYSTEM_VOLUME_NAMES]
    else:
        home = env.get("HOME") or env.get("USERPROFILE")
        for value in ("/bin", "/boot", "/dev", "/etc", "/lib", "/lib32", "/lib64", "/libx32", "/proc", "/root", "/run", "/sbin",
                      "/sys", "/usr", "/opt", "/snap", "/System", "/Library", "/Applications", "/private/etc", "/var/lib", "/var/log"):
            add(inside, value, "system_folder")
        for value in ("/", "/home", "/Users", "/mnt", "/media", "/Volumes"):
            add(exact, value, "users_folder" if value in ("/home", "/Users") else "drive_root")
        profile = home
        if home:
            parent = k(str(PurePosixPath(home).parent))
            users.append(parent)  # type: ignore[arg-type]
            if parent and len(parent) > 1:  # the directory holding every profile is refused too, like /home itself
                exact.append((parent, "users_folder"))
            add(exact, home, "profile_root")
        else:
            users.extend([k("/home"), k("/Users")])  # type: ignore[list-item]
        names = [(n, "credentials") for n in CREDENTIAL_NAMES]
    # A "users directory" that is itself a drive/filesystem root (profile at C:\me or /root) would make every path
    # look like another user's profile, so it is ignored.
    users_keys = tuple(u for u in dict.fromkeys(users) if u and len(u) > 1)
    return PolicyEnvironment(windows, tuple(exact), tuple(inside), users_keys, k(profile), tuple((_key(n, windows), c) for n, c in names))


def current_environment() -> PolicyEnvironment:
    windows = os.name == "nt"
    env = dict(os.environ)
    if not windows and not env.get("HOME"):
        env["HOME"] = os.path.expanduser("~")
    return build_environment(windows, env)


def classify(path: str, env: PolicyEnvironment) -> str | None:
    """Refusal code for an absolute, prefix-free path, or None when the location is acceptable."""
    key, unc = _keys(path, env.windows)
    if unc:
        return "unc"
    if len(key) == 1:
        return "drive_root"
    for base, code in env.exact:
        if key == base:
            return code
    for base, code in env.inside:
        if _within(key, base):
            return code
    for users in env.users_dirs:
        if len(key) > len(users) and _within(key, users):
            if env.profile is None or key[: len(users) + 1] != env.profile[: len(users) + 1]:
                return "other_profile"
    name_codes = dict(env.names)
    for component in key[1:]:
        if component in name_codes:
            return name_codes[component]
    return None


def nested_reason(path: str, env: PolicyEnvironment) -> str | None:
    """Why a directory strictly INSIDE an already accepted workspace must be skipped (location rules are not repeated:
    the workspace itself may legitimately live below a folder that is protected as a workspace root)."""
    key, _ = _keys(path, env.windows)
    if len(key) < 2:
        return None
    name_codes = dict(env.names)
    if key[-1] in name_codes:
        return name_codes[key[-1]]
    for base, code in env.inside:
        if key == base:
            return code
    for users in env.users_dirs:
        if len(key) == len(users) + 1 and _within(key, users) and (env.profile is None or key != env.profile):
            return "other_profile"
    return None


def protected_chain(root: Path, file: Path, env: PolicyEnvironment | None = None) -> str | None:
    """Refusal code if `file` sits inside a protected subfolder of `root` (checks each directory between them)."""
    env = env or current_environment()
    try:
        relative = file.relative_to(root)
    except ValueError:
        return None
    current = root
    for part in relative.parts[:-1]:
        current = current / part
        reason = nested_reason(str(current), env)
        if reason:
            return reason
    return None


def drive_is_remote(path: str) -> bool:
    """Windows only: True for a drive letter that maps to a network share. (POSIX network mounts are not detected.)"""
    if os.name != "nt":
        return False
    try:
        anchor = PureWindowsPath(path).anchor
        return bool(anchor) and ctypes.windll.kernel32.GetDriveTypeW(anchor) == 4  # DRIVE_REMOTE
    except (OSError, AttributeError, ValueError):
        return False


def check_workspace(raw: str, app_db: Path | None = None, env: PolicyEnvironment | None = None) -> Path:
    """Return the canonical workspace directory, or raise WorkspaceRefused / WorkspaceMissing / WorkspaceNotDirectory."""
    env = env or current_environment()
    windows = env.windows
    normalized = normalize_raw(os.path.expanduser(raw) if isinstance(raw, str) else raw, windows)
    # 1) lexical check: independent of whether the path exists, so protected paths never leak existence
    code = classify(normalized, env)
    if code:
        raise WorkspaceRefused(code)
    # 2) resolve symlinks, junctions, short names, case, mapped drives
    try:
        resolved = Path(normalized).resolve(strict=True)
    except PermissionError as error:
        raise WorkspaceRefused("inaccessible") from error
    except OSError as error:  # missing, a path component that is a file, an invalid name, too long, ...
        raise WorkspaceMissing from error
    canonical = normalize_raw(str(resolved), windows)  # strips \\?\ ; refuses UNC / volume paths the OS resolved to
    code = classify(canonical, env)
    if code:
        raise WorkspaceRefused(code)
    if windows and drive_is_remote(canonical):
        raise WorkspaceRefused("network_drive")
    if not resolved.is_dir():
        raise WorkspaceNotDirectory
    # 3) never a folder that contains the application's own database
    if app_db is not None:
        try:
            database = normalize_raw(str(Path(app_db).resolve()), windows)
        except WorkspaceRefused:
            database = None
        if database and _within(_keys(database, windows)[0], _keys(canonical, windows)[0]):
            raise WorkspaceRefused("app_database")
    return Path(canonical)

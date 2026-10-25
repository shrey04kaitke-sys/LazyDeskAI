"""Create, reset or remove an isolated, synthetic LazyDesk demo workspace and database.

Standard library only. Everything lives under one folder named LazyDeskDemo (default:
%USERPROFILE%\\LazyDeskDemo) that is marked with a marker file. The commands refuse to touch any other
folder, so they cannot reach personal data or the real backend/lazydesk.db.

    python demo_workspace.py setup  [--base PATH]   create the workspace (rebuilds an existing demo)
    python demo_workspace.py reset  [--base PATH]   delete workspace + demo DB, then recreate the original files
    python demo_workspace.py remove [--base PATH]   delete workspace + demo DB + marker (and the folder if empty)
    python demo_workspace.py info   [--base PATH]   print the paths as JSON
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

DEMO_DIR_NAME = "LazyDeskDemo"
MARKER_NAME = ".lazydesk-demo"
MARKER_TEXT = "LazyDesk synthetic demo data. Managed by demo_workspace.py; contains no personal files.\n"
WORKSPACE_NAME = "workspace"
DB_NAME = "demo.sqlite"
DB_SIDECARS = ("", "-wal", "-shm")
FIXED_MTIME = 1_714_550_400  # 2024-05-01, so repeated builds are identical
BLOCKED_PARTS = {"onedrive", "desktop", "downloads", "documents"}
REPARSE_POINT = 0x400


class DemoSafetyError(Exception):
    """Raised when an operation would touch anything other than a LazyDesk demo folder."""


def default_base() -> Path:
    # Directly under the profile, not AppData: LazyDesk refuses AppData as a workspace location.
    return Path.home() / DEMO_DIR_NAME


def is_link_like(path: Path) -> bool:
    try:
        return path.is_symlink() or bool(getattr(path.lstat(), "st_file_attributes", 0) & REPARSE_POINT)
    except OSError:
        return False


def validate_base(base: Path | str) -> Path:
    candidate = Path(base).expanduser()
    if candidate.name != DEMO_DIR_NAME:
        raise DemoSafetyError(f"Refusing to use '{candidate}': the demo folder must be named {DEMO_DIR_NAME}.")
    if is_link_like(candidate):
        raise DemoSafetyError(f"Refusing to use '{candidate}': it is a link or junction.")
    resolved = candidate.parent.resolve() / candidate.name
    home = Path.home().resolve()
    lowered = {part.lower() for part in resolved.parts}
    if resolved == resolved.parent or resolved == home or resolved in home.parents:
        raise DemoSafetyError(f"Refusing to use '{resolved}': it is a drive root or contains your home folder.")
    if lowered & BLOCKED_PARTS:
        raise DemoSafetyError(f"Refusing to use '{resolved}': demo data must not live in OneDrive, Desktop, Downloads or Documents.")
    if resolved.exists():
        if not resolved.is_dir():
            raise DemoSafetyError(f"Refusing to use '{resolved}': it is not a folder.")
        if any(resolved.iterdir()) and not (resolved / MARKER_NAME).is_file():
            raise DemoSafetyError(f"Refusing to use '{resolved}': it is not empty and is not a LazyDesk demo folder (no {MARKER_NAME}).")
    return resolved


def paths(base: Path) -> dict[str, Path]:
    return {"base": base, "workspace": base / WORKSPACE_NAME, "database": base / DB_NAME, "marker": base / MARKER_NAME}


def text(*lines: str) -> bytes:
    return ("\n".join(lines) + "\n").encode("utf-8")


def placeholder(kind: str, label: str) -> bytes:
    return text(f"LazyDesk synthetic {kind} placeholder: {label}", "This file is generated demo data, not a real document.")


def demo_files() -> dict[str, bytes]:
    invoice = placeholder("PDF", "invoice March")
    budget = text("item,amount", "rent,1200", "food,400", "travel,150")
    report = placeholder("DOCX", "quarterly report")
    logo = placeholder("PNG", "company logo")
    return {
        # Inbox: a typical messy downloads-style folder
        "Inbox/invoice_2024_03.pdf": invoice,
        "Inbox/invoice_2024_03 (1).pdf": invoice,  # exact duplicate of the line above
        "Inbox/meeting-notes.txt": text("Agenda", "1. Roadmap", "2. Budget"),
        "Inbox/budget.csv": budget,
        "Inbox/beach-photo.jpg": placeholder("JPEG", "beach photo"),
        "Inbox/screenshot 2024-05-01.png": placeholder("PNG", "screenshot"),
        "Inbox/setup-installer.zip": placeholder("ZIP", "installer"),
        "Inbox/backup-script.py": text("print('synthetic demo script')"),
        "Inbox/voice-memo.mp3": placeholder("MP3", "voice memo"),
        "Inbox/holiday-clip.mp4": placeholder("MP4", "holiday clip"),
        # Projects: nested, with cross-folder duplicates and same-named but different files
        "Projects/Alpha/report.docx": report,
        "Projects/Alpha/report-final-FINAL.docx": report,  # duplicate under a worse name
        "Projects/Alpha/budget-copy.csv": budget,  # duplicate of Inbox/budget.csv in another folder
        "Projects/Alpha/notes.txt": text("Alpha notes", "kickoff on Monday"),
        "Projects/Alpha/assets/logo.png": logo,
        "Projects/Alpha/assets/logo-copy.png": logo,
        "Projects/Beta/notes.txt": text("Beta notes", "different content, same file name as Alpha/notes.txt"),
        "Projects/Beta/design.svg": text("<svg xmlns='http://www.w3.org/2000/svg' width='8' height='8'/>"),
        # Archive: files with no deterministic rule (where an optional local model can help)
        "Archive/2023/old-notes.txt": text("Notes from 2023"),
        "Archive/2023/mystery.xyz": placeholder("unknown-type", "mystery"),
        "Archive/2023/data.weird": placeholder("unknown-type", "weird data"),
        "Archive/2023/readme": text("a file with no extension"),
    }


def _remove_tree(target: Path, base: Path) -> None:
    if not target.exists() and not is_link_like(target):
        return
    if target.parent != base or is_link_like(target):
        raise DemoSafetyError(f"Refusing to delete '{target}'.")
    shutil.rmtree(target)


def _remove_database(base: Path) -> None:
    for suffix in DB_SIDECARS:
        file = base / f"{DB_NAME}{suffix}"
        if file.is_file() and not is_link_like(file):
            file.unlink()


def _wipe(base: Path) -> None:
    _remove_tree(base / WORKSPACE_NAME, base)
    _remove_database(base)


def build(base: Path | str) -> dict[str, object]:
    resolved = validate_base(base)
    resolved.mkdir(parents=True, exist_ok=True)
    _wipe(resolved)
    (resolved / MARKER_NAME).write_text(MARKER_TEXT, encoding="utf-8")
    workspace = resolved / WORKSPACE_NAME
    files = demo_files()
    for relative, content in files.items():
        target = workspace / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        os.utime(target, (FIXED_MTIME, FIXED_MTIME))
    return {**{key: str(value) for key, value in paths(resolved).items()}, "files": len(files)}


def remove(base: Path | str) -> dict[str, object]:
    resolved = validate_base(base)
    if not resolved.exists():
        return {"removed": False, "reason": "nothing to remove"}
    if not (resolved / MARKER_NAME).is_file():
        raise DemoSafetyError(f"Refusing to remove '{resolved}': no {MARKER_NAME} marker.")
    _wipe(resolved)
    (resolved / MARKER_NAME).unlink()
    folder_removed = False
    if not any(resolved.iterdir()):  # anything else in the folder is not ours: keep it
        resolved.rmdir()
        folder_removed = True
    return {"removed": True, "folder_removed": folder_removed}


def info(base: Path | str) -> dict[str, str]:
    return {key: str(value) for key, value in paths(validate_base(base)).items()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["setup", "reset", "remove", "info"])
    parser.add_argument("--base", default=None, help=f"demo folder (must be named {DEMO_DIR_NAME})")
    arguments = parser.parse_args(argv)
    base = arguments.base or default_base()
    try:
        if arguments.command in ("setup", "reset"):
            result = build(base)
        elif arguments.command == "remove":
            result = remove(base)
        else:
            result = info(base)
    except DemoSafetyError as error:
        print(f"SAFETY STOP: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

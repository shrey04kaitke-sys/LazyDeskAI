"""One backend process per database.

Crash recovery and the in-memory run registry assume that no other process is executing runs against the same database,
so a second backend on that database is refused at startup, BEFORE any recovery runs. Ownership is an exclusive operating
system lock (msvcrt byte-range lock on Windows, fcntl.flock elsewhere) on a lock file beside the database:

  * the OS lock, not the file's existence, decides who owns the database; the file is never deleted;
  * the OS releases the lock when the owning process exits for any reason (normal exit, crash, kill), so there is nothing
    stale to clean up;
  * it is held for the lifetime of the process and released explicitly on shutdown or startup failure.

Limits: advisory/range locks on network shares and some unusual filesystems can behave differently (or be unsupported); in
that case acquisition fails and startup is refused rather than continuing unprotected. Two spellings of one database path
(relative, symlinked, different case on Windows) resolve to one lock file; hard links to a database from other directories
are not detected."""
from __future__ import annotations

import os
from pathlib import Path

LOCK_SUFFIX = ".lock"


class DatabaseLockError(RuntimeError):
    """The database cannot be locked, so the backend must not start."""


def lock_path_for(database: Path | str) -> Path:
    """`<database>.lock` in the database's own directory, derived from the resolved path so every spelling maps to one file."""
    try:
        resolved = Path(database).expanduser().resolve()
    except (OSError, RuntimeError, ValueError) as error:
        raise DatabaseLockError(f"The database path {str(database)[:200]!r} is not usable ({error.__class__.__name__}); refusing to start.") from None
    try:
        if resolved.is_dir():
            raise DatabaseLockError(f"The database path {resolved} is a directory, not a file; refusing to start.")
        if not resolved.parent.is_dir():
            raise DatabaseLockError(f"The folder for the database ({resolved.parent}) does not exist; refusing to start.")
    except ValueError:
        raise DatabaseLockError("The database path contains an invalid character; refusing to start.") from None
    return resolved.with_name(resolved.name + LOCK_SUFFIX)


class DatabaseLock:
    def __init__(self, database: Path | str):
        self.database = Path(database)
        self._fd: int | None = None
        self.path: Path | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> "DatabaseLock":
        if self._fd is not None:
            return self
        path = lock_path_for(self.database)
        try:
            unsafe = path.is_symlink() or path.is_dir()
        except ValueError:
            unsafe = True
        if unsafe:
            raise DatabaseLockError(f"The lock file {path} is a link or a directory; refusing to start without database protection.")
        try:
            fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
        except (OSError, ValueError) as error:
            raise DatabaseLockError(
                f"Cannot create or open the database lock file {str(path)[:200]!r} ({getattr(error, 'strerror', None) or error.__class__.__name__}); "
                "refusing to start without database protection. Check the folder's permissions.") from None
        try:
            _lock(fd)
        except OSError:
            os.close(fd)
            raise DatabaseLockError(
                f"Another LazyDesk backend (or the lock on this filesystem) is already holding the database {path.with_name(path.name[:-len(LOCK_SUFFIX)])}. "
                "Only one backend may use a database at a time. Stop the other backend, or start this one with a different database "
                "(LAZYDESK_DB). Nothing was changed.") from None
        self._fd, self.path = fd, path
        return self

    def release(self) -> None:
        """Release the OS lock (the file stays; the lock, not the file, is the ownership). Safe to call twice."""
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            _unlock(fd)
        except OSError:
            pass
        finally:
            os.close(fd)

    def __enter__(self) -> "DatabaseLock":
        return self.acquire()

    def __exit__(self, *_exc) -> None:
        self.release()


if os.name == "nt":
    import msvcrt

    def _lock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)   # non-blocking; raises OSError if another handle holds byte 0

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)

"""Durable, atomic file writes.

Everything torsh persists — the config, the id map, libtorrent's session
state and above all the fast-resume blobs — is written the same way: into a
unique temporary file beside the target, flushed all the way down to the
disk, then renamed over it.

Both halves matter, and they guard different failures:

* ``os.replace`` is atomic, so no reader ever observes a half-written file
  and a crash mid-write leaves the *previous* version intact;
* the ``fsync`` before it is what makes the new contents survive a power
  cut rather than just a crashed process — without it, a rename can land
  on disk ahead of the data it points at, and the file comes back empty.
  "Crash-safe resume" is a promise about exactly that.

The temporary name carries the pid, so two processes racing to save the same
file (the daemon persisting speed limits while the TUI persists its sort
order) can never scribble over each other's staging file.

Files are created private (0600) by default: resume data says what you are
downloading and the endpoint file holds an auth token.  Callers writing
something the user is meant to share — ``torsh export`` — ask for 0644.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path

PRIVATE = 0o600
SHARED = 0o644

# Staging files are hidden and suffixed so a reader that globs for real names
# (``*.resume``) skips them, and a sweep can recognise its own leftovers.
_TMP_PREFIX = "."
_TMP_SUFFIX = ".tmp"


def write_bytes(path: Path, data: bytes, *, mode: int = PRIVATE) -> None:
    """Write *data* to *path* atomically and durably.

    Creates the parent directory if needed.  Raises :class:`OSError` — the
    same failures a plain write would produce, on the same call.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(
        dir=path.parent, prefix=f"{_TMP_PREFIX}{path.name}.{os.getpid()}.", suffix=_TMP_SUFFIX
    )
    tmp = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        # mkstemp already created it 0600; widen only when asked to.
        if mode != PRIVATE:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        # Includes KeyboardInterrupt/SystemExit: never leave staging litter.
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    _sync_dir(path.parent)


def write_text(path: Path, text: str, *, mode: int = PRIVATE) -> None:
    """UTF-8 flavour of :func:`write_bytes`."""
    write_bytes(path, text.encode("utf-8"), mode=mode)


def sweep_stale(directory: Path) -> int:
    """Delete staging files a hard kill left behind; returns how many.

    :func:`write_bytes` cleans up after itself on any exception, so this only
    ever finds litter from SIGKILL or a power cut.
    """
    removed = 0
    with contextlib.suppress(OSError):
        for leftover in directory.glob(f"{_TMP_PREFIX}*{_TMP_SUFFIX}"):
            with contextlib.suppress(OSError):
                leftover.unlink()
                removed += 1
    return removed


def _sync_dir(directory: Path) -> None:
    """Persist the rename itself, best effort.

    A durable file inside a directory entry that never reached disk is still
    a lost file.  Directory fsync is a no-op or an error on some platforms
    (Windows, a few network filesystems), and failing there must not turn a
    successful write into an exception.
    """
    with contextlib.suppress(OSError, AttributeError):
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

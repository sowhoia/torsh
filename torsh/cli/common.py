"""Shared CLI plumbing: error handling, daemon access, selector resolution."""

from __future__ import annotations

import contextlib
import functools
import os
import sys
from collections.abc import Callable
from typing import Any

from .. import config as config_mod
from .. import ipc
from ..select import SelectorError, resolve_many
from . import render

# Shell convention for "killed by SIGPIPE" (128 + 13) and SIGINT (128 + 2).
_EXIT_SIGPIPE = 141
_EXIT_INTERRUPTED = 130


def handle_errors(func: Callable[..., Any]) -> Callable[..., Any]:
    """Turn expected failures into one clean line + exit 1 (no tracebacks)."""

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except ipc.DaemonUnavailable as exc:
            render.fail(f"{exc}")
            render.econsole().print("[dim]  start it with[/] [bold]torsh daemon start[/]")
            sys.exit(1)
        except (ipc.RpcError, SelectorError) as exc:
            render.fail(str(exc))
            sys.exit(1)
        except BrokenPipeError:
            # `torsh ls --json | head`: the reader closed the pipe. Point what
            # is left of stdout at /dev/null so the interpreter's shutdown
            # flush can't append "Exception ignored…", then exit as SIGPIPE.
            with contextlib.suppress(OSError, ValueError):
                os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
            sys.exit(_EXIT_SIGPIPE)
        except OSError as exc:
            # An unwritable state dir, a foreign runtime dir, a full disk:
            # the user's environment, not a torsh bug — no traceback.
            render.fail(str(exc))
            sys.exit(1)
        except KeyboardInterrupt:
            sys.exit(_EXIT_INTERRUPTED)

    return wrapper


def client() -> ipc.Client:
    """Connect to the daemon, autostarting it when the config allows."""
    config = config_mod.load()
    return ipc.ensure_daemon(autostart=config.daemon.autostart)


def pick(cli: ipc.Client, selectors: tuple[str, ...] | list[str]) -> list[dict[str, Any]]:
    """Resolve selectors against the live torrent list."""
    torrents = cli.call("list")
    if not torrents:
        raise SelectorError("no torrents yet — add one with `torsh add`")
    return resolve_many(list(selectors), torrents)


def hashes(torrents: list[dict[str, Any]]) -> list[str]:
    return [t["hash"] for t in torrents]


def summarize(torrents: list[dict[str, Any]], limit: int = 3) -> str:
    names = [t["name"] if len(t["name"]) <= 40 else t["name"][:37] + "…" for t in torrents[:limit]]
    extra = len(torrents) - limit
    if extra > 0:
        names.append(f"+{extra} more")
    return ", ".join(names)

"""Humane torrent selectors.

``torsh pause 3``, ``torsh rm ubuntu``, ``torsh resume all`` — every command
that targets torrents accepts, in resolution order:

1. the integer id shown in ``torsh ls``;
2. an infohash or unique infohash prefix (≥ 4 chars);
3. a group word: ``all``, ``active``, ``paused``, ``done``, ``seeding``,
   ``downloading``, ``errored``;
4. a case-insensitive name substring — it must match exactly one torrent,
   otherwise the error lists the candidates so the user can disambiguate.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

Torrent = dict[str, Any]

# Insertion order is the order users see in `--status` help and in the TUI's
# filter cycle, so it reads widest-first rather than alphabetically.
GROUPS: dict[str, Callable[[Torrent], bool]] = {
    "all": lambda t: True,
    "active": lambda t: t["status"] in {"downloading", "seeding", "metadata", "checking", "moving"},
    "downloading": lambda t: t["status"] == "downloading",
    "seeding": lambda t: t["status"] == "seeding",
    "paused": lambda t: t["status"] in {"paused", "queued"},
    "done": lambda t: t["progress"] >= 1.0,
    "errored": lambda t: t["status"] == "error",
}

# One sort-key registry for every surface (CLI `ls --sort`, TUI columns),
# so the same key always orders torrents the same way.
SORT_KEYS: dict[str, Callable[[Torrent], Any]] = {
    "id": lambda t: t["id"],
    "added": lambda t: t["added"],
    "name": lambda t: t["name"].lower(),
    "size": lambda t: t["size"],
    "progress": lambda t: t["progress"],
    "down": lambda t: t["down_rate"],
    "up": lambda t: t["up_rate"],
    "ratio": lambda t: t["ratio"],
    "eta": lambda t: t["eta"] if t["eta"] >= 0 else float("inf"),
}


class SelectorError(Exception):
    """Selector did not resolve; the message is ready to show the user."""


def _is_hexish(text: str) -> bool:
    return len(text) >= 4 and all(c in "0123456789abcdef" for c in text.lower())


def resolve(selector: str, torrents: list[Torrent]) -> list[Torrent]:
    """Resolve one selector to a non-empty list of torrents (or raise).

    Each stage returns an empty list to mean "not my kind of selector" and
    raises to mean "mine, but nothing matched" — which is what decides
    whether the cascade continues.
    """
    text = selector.strip()
    if not text:
        raise SelectorError("empty selector")
    for stage in (_by_group, _by_id, _by_hash):
        matched = stage(text, torrents)
        if matched:
            return matched
    return _by_name(text, torrents)  # the last stage; it raises or resolves


def _by_group(text: str, torrents: list[Torrent]) -> list[Torrent]:
    """A group word (`all`, `paused`, `done`, …) — a whole class at once."""
    group = GROUPS.get(text.lower())
    if group is None:
        return []
    matched = [t for t in torrents if group(t)]
    if not matched:
        raise SelectorError(f"no torrents match '{text}'")
    return matched


def _by_id(text: str, torrents: list[Torrent]) -> list[Torrent]:
    """The integer shown in ``torsh ls``.

    Never falls through to the name stage: a wrong number is a typo, and
    quietly treating ``5`` as a name fragment could act on the wrong torrent.
    """
    if not text.isdigit():
        return []
    wanted = int(text)
    matched = [t for t in torrents if t["id"] == wanted]
    if not matched:
        raise SelectorError(f"no torrent with id {wanted} — see `torsh ls`")
    return matched


def _by_hash(text: str, torrents: list[Torrent]) -> list[Torrent]:
    """An infohash or unique infohash prefix.

    Falls through when nothing matches — short hex strings are perfectly
    ordinary name fragments too ("ac3", "dbed").
    """
    if not _is_hexish(text):
        return []
    matched = [t for t in torrents if t["hash"].startswith(text.lower())]
    if len(matched) > 1:
        raise SelectorError(f"hash prefix '{text}' is ambiguous ({len(matched)} matches)")
    return matched


def _by_name(text: str, torrents: list[Torrent]) -> list[Torrent]:
    """A case-insensitive name substring, which must match exactly one.

    The final stage, so this is also where an unrecognised selector fails.
    """
    needle = text.lower()
    matched = [t for t in torrents if needle in t["name"].lower()]
    if len(matched) == 1:
        return matched
    if not matched:
        raise SelectorError(f"nothing matches '{text}' — see `torsh ls`")
    names = ", ".join(f"#{t['id']} {t['name'][:40]}" for t in matched[:5])
    more = "…" if len(matched) > 5 else ""
    raise SelectorError(f"'{text}' matches {len(matched)} torrents: {names}{more}")


def resolve_many(selectors: list[str], torrents: list[Torrent]) -> list[Torrent]:
    """Resolve several selectors, deduplicated, preserving order."""
    seen: set[str] = set()
    result: list[Torrent] = []
    for selector in selectors:
        for torrent in resolve(selector, torrents):
            if torrent["hash"] not in seen:
                seen.add(torrent["hash"])
                result.append(torrent)
    return result

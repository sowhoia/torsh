"""Selector resolution: ids, hash prefixes, groups, name substrings."""

from __future__ import annotations

import pytest

from torsh.select import SORT_KEYS, SelectorError, resolve, resolve_many


def t(id, hash, name, status="downloading", progress=0.5):
    return {"id": id, "hash": hash, "name": name, "status": status, "progress": progress}


TORRENTS = [
    t(1, "aaaa11" + "0" * 34, "Ubuntu 24.04 LTS", "downloading", 0.4),
    t(2, "aaaa22" + "1" * 34, "Fedora Workstation", "seeding", 1.0),
    t(3, "bbbb33" + "2" * 34, "Arch ISO", "paused", 1.0),
    t(4, "cccc44" + "3" * 34, "ubuntu server", "error", 0.2),
]


def test_resolve_by_id():
    assert resolve("2", TORRENTS)[0]["name"] == "Fedora Workstation"


def test_resolve_by_unique_hash_prefix():
    assert resolve("bbbb", TORRENTS)[0]["id"] == 3


def test_ambiguous_hash_prefix_raises():
    with pytest.raises(SelectorError, match="ambiguous"):
        resolve("aaaa", TORRENTS)


def test_resolve_groups():
    assert len(resolve("all", TORRENTS)) == 4
    assert [x["id"] for x in resolve("paused", TORRENTS)] == [3]
    assert [x["id"] for x in resolve("done", TORRENTS)] == [2, 3]
    assert [x["id"] for x in resolve("errored", TORRENTS)] == [4]
    assert [x["id"] for x in resolve("seeding", TORRENTS)] == [2]


def test_resolve_by_name_substring():
    assert resolve("fedora", TORRENTS)[0]["id"] == 2
    assert resolve("arch", TORRENTS)[0]["id"] == 3


def test_ambiguous_name_lists_candidates():
    with pytest.raises(SelectorError, match="matches 2 torrents"):
        resolve("ubuntu", TORRENTS)


def test_unknown_selector():
    with pytest.raises(SelectorError, match="nothing matches"):
        resolve("windows", TORRENTS)
    with pytest.raises(SelectorError, match="no torrent with id"):
        resolve("99", TORRENTS)


def test_resolve_many_dedupes():
    picked = resolve_many(["1", "aaaa1", "fedora"], TORRENTS)
    assert [x["id"] for x in picked] == [1, 2]


def test_empty_selector_is_rejected():
    with pytest.raises(SelectorError, match="empty selector"):
        resolve("   ", TORRENTS)


def test_a_hash_prefix_that_matches_nothing_falls_through_to_names():
    """Short hex strings are ordinary name fragments too."""
    hexish = [t(1, "1" * 40, "The ac3 remux"), t(2, "2" * 40, "Something else")]
    assert resolve("ac3", hexish)[0]["id"] == 1


def test_a_wrong_id_never_falls_through_to_names():
    """`torsh rm 5` with no #5 must fail, not quietly remove "Episode 5"."""
    with pytest.raises(SelectorError, match="no torrent with id"):
        resolve("5", [t(1, "a" * 40, "Episode 5")])


def test_group_words_win_over_name_matches():
    named_all = [t(1, "a" * 40, "all about ducks"), t(2, "b" * 40, "geese", status="paused")]
    assert len(resolve("all", named_all)) == 2


def test_sort_keys_cover_every_column_and_order_sensibly():
    rows = [
        {**t(1, "a" * 40, "b"), "added": 2, "size": 10, "down_rate": 0, "up_rate": 0, "ratio": 0.5, "eta": -1},
        {**t(2, "b" * 40, "a"), "added": 1, "size": 20, "down_rate": 5, "up_rate": 1, "ratio": 1.5, "eta": 30},
    ]
    assert [r["id"] for r in sorted(rows, key=SORT_KEYS["name"])] == [2, 1]
    assert [r["id"] for r in sorted(rows, key=SORT_KEYS["size"])] == [1, 2]
    # An unknown ETA sorts last, not first — "—" is not "arriving now".
    assert [r["id"] for r in sorted(rows, key=SORT_KEYS["eta"])] == [2, 1]

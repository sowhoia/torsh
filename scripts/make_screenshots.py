"""Regenerate the README screenshots with a real TorshApp.

The UI is 100% real — themes, rendering, selection, the command palette —
only the daemon behind it is a canned :class:`FakeClient`, so frames are
deterministic and always photogenic. Run from the repo root:

    .venv/bin/python scripts/make_screenshots.py

SVG frames land in docs/assets/; convert the GIF frames with
`scripts/svg2gif.sh` (qlmanage + Pillow) afterwards.
"""

from __future__ import annotations

import asyncio
import os
import random
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ASSETS = ROOT / "docs" / "assets"
# 172 columns: wide table mode (≥128 after the 44-col details panel) plus the
# panel itself — the full 11-column showcase in one frame. 34 rows keeps the
# composition tight around a 9-torrent library.
WIDTH, HEIGHT = 172, 34

GIB, MIB, KIB = 1024**3, 1024**2, 1024


def h(hexidecimal: str, n: int = 40) -> str:
    return (hexidecimal * n)[:n]


# ---------------------------------------------------------------------------
# The demo library — plausible names, sizes, speeds, mixed statuses
# ---------------------------------------------------------------------------

UBUNTU = h("a1b2c3d4e5", 40)
ALBUM = h("f00dc0ffee", 40)

TORRENTS = [
    {
        "id": 1, "hash": UBUNTU,
        "name": "ubuntu-24.04.3-desktop-amd64.iso",
        "status": "downloading", "progress": 0.614,
        "size": 6 * GIB, "done": int(6 * GIB * 0.614),
        "downloaded": int(6 * GIB * 0.614), "uploaded": int(1.2 * GIB),
        "down_rate": int(11.8 * MIB), "up_rate": int(412 * KIB),
        "eta": 192, "ratio": 0.18, "peers": 23, "seeds": 41,
        "sequential": False, "has_metadata": True, "queue": -1,
        "save_path": "/home/mira/Downloads/torsh",
    },
    {
        "id": 2, "hash": h("b2", 40),
        "name": "Big.Buck.Bunny.2008.1080p.OpenMovie.mkv",
        "status": "seeding", "progress": 1.0,
        "size": 895 * MIB, "done": 895 * MIB,
        "downloaded": 895 * MIB, "uploaded": int(2.1 * GIB),
        "down_rate": 0, "up_rate": int(2.9 * MIB),
        "eta": 0, "ratio": 2.414, "peers": 8, "seeds": 112,
        "sequential": False, "has_metadata": True, "queue": -1,
        "save_path": "/home/mira/Downloads/torsh",
    },
    {
        "id": 3, "hash": h("c3", 40),
        "name": "debian-13.2.0-amd64-netinst.iso",
        "status": "seeding", "progress": 1.0,
        "size": 657 * MIB, "done": 657 * MIB,
        "downloaded": 657 * MIB, "uploaded": int(736 * MIB),
        "down_rate": 0, "up_rate": int(638 * KIB),
        "eta": 0, "ratio": 1.121, "peers": 3, "seeds": 204,
        "sequential": False, "has_metadata": True, "queue": -1,
        "save_path": "/home/mira/Downloads/torsh",
    },
    {
        "id": 4, "hash": h("d4", 40),
        "name": "blender-4.5.3-linux-x64.tar.xz",
        "status": "downloading", "progress": 0.892,
        "size": 312 * MIB, "done": int(312 * MIB * 0.892),
        "downloaded": int(312 * MIB * 0.892), "uploaded": int(89 * MIB),
        "down_rate": int(2.2 * MIB), "up_rate": int(97 * KIB),
        "eta": 41, "ratio": 0.29, "peers": 9, "seeds": 17,
        "sequential": False, "has_metadata": True, "queue": -1,
        "save_path": "/home/mira/Downloads/torsh",
    },
    {
        "id": 5, "hash": ALBUM,
        "name": "Radiohead.OK.Computer.1997.FLAC.Lossless",
        "status": "downloading", "progress": 0.47,
        "size": 412 * MIB, "done": int(412 * MIB * 0.47),
        "downloaded": int(412 * MIB * 0.47), "uploaded": int(21 * MIB),
        "down_rate": int(384 * KIB), "up_rate": int(56 * KIB),
        "eta": 615, "ratio": 0.05, "peers": 11, "seeds": 6,
        "sequential": False, "has_metadata": True, "queue": -1,
        "save_path": "/home/mira/Downloads/torsh",
    },
    {
        "id": 6, "hash": h("e5", 40),
        "name": "Sintel.2010.1080p.OpenMovie.mkv",
        "status": "seeding", "progress": 1.0,
        "size": 1.1 * GIB, "done": int(1.1 * GIB),
        "downloaded": int(1.1 * GIB), "uploaded": int(1.7 * GIB),
        "down_rate": 0, "up_rate": int(512 * KIB),
        "eta": 0, "ratio": 1.552, "peers": 5, "seeds": 64,
        "sequential": False, "has_metadata": True, "queue": -1,
        "save_path": "/home/mira/Downloads/torsh",
    },
    {
        "id": 7, "hash": h("f6", 40),
        "name": "archlinux-x86_64-2026.08.01.iso",
        "status": "paused", "progress": 0.341,
        "size": 1.4 * GIB, "done": int(1.4 * GIB * 0.341),
        "downloaded": int(1.4 * GIB * 0.341), "uploaded": int(52 * MIB),
        "down_rate": 0, "up_rate": 0,
        "eta": -1, "ratio": 0.04, "peers": 0, "seeds": 0,
        "sequential": False, "has_metadata": True, "queue": -1,
        "save_path": "/home/mira/Downloads/torsh",
    },
    {
        "id": 8, "hash": h("a7", 40),
        "name": "nixos-25.05-minimal-x86_64.iso",
        "status": "queued", "progress": 0.0,
        "size": 0, "done": 0, "downloaded": 0, "uploaded": 0,
        "down_rate": 0, "up_rate": 0,
        "eta": -1, "ratio": 0.0, "peers": 0, "seeds": 0,
        "sequential": False, "has_metadata": False, "queue": 1,
        "save_path": "/home/mira/Downloads/torsh",
    },
    {
        "id": 9, "hash": h("b8", 40),
        "name": "fedora-43-workstation-live-x86_64.iso",
        "status": "metadata", "progress": 0.0,
        "size": 0, "done": 0, "downloaded": 0, "uploaded": 0,
        "down_rate": 0, "up_rate": 0,
        "eta": -1, "ratio": 0.0, "peers": 14, "seeds": 33,
        "sequential": False, "has_metadata": False, "queue": -1,
        "save_path": "/home/mira/Downloads/torsh",
    },
]

SESSION = {
    "torrents": len(TORRENTS), "active": 4, "paused": 1,
    "down_rate": int(14.4 * MIB), "up_rate": int(4.1 * MIB),
    "downloaded": int(4.4 * GIB), "uploaded": int(5.6 * GIB),
    "dht_nodes": 386, "listen_port": 51421,
    "limits": {"down": int(16 * MIB), "up": int(4 * MIB)},
    "uptime": 4021, "engine": "libtorrent 2.0.11",
}

TRACKS = [
    ("01 Airbag.flac", 31 * MIB, 1.0), ("02 Paranoid Android.flac", 52 * MIB, 1.0),
    ("03 Subterranean Homesick Alien.flac", 40 * MIB, 1.0),
    ("04 Exit Music (For a Film).flac", 35 * MIB, 1.0), ("05 Let Down.flac", 41 * MIB, 1.0),
    ("06 Karma Police.flac", 37 * MIB, 1.0), ("07 Fitter Happier.flac", 8 * MIB, 1.0),
    ("08 Electioneering.flac", 33 * MIB, 0.78), ("09 Climbing Up the Walls.flac", 38 * MIB, 0.31),
    ("10 No Surprises.flac", 33 * MIB, 0.0), ("11 Lucky.flac", 36 * MIB, 0.0),
    ("12 The Tourist.flac", 40 * MIB, 0.0), ("cover.jpg", 112 * KIB, 1.0),
    ("info.txt", 2 * KIB, 1.0),
]

PEERS = [
    ("84.17.52.107:51413", "qBittorrent v5.1.2", int(3.1 * MIB), int(210 * KIB), 0.62),
    ("91.132.0.44:6881", "Transmission 4.0.6", int(1.9 * MIB), 0, 0.58),
    ("185.220.101.7:51415", "libtorrent/2.0.11", int(1.4 * MIB), int(64 * KIB), 0.61),
    ("45.61.183.9:6889", "Deluge/2.1.1", int(980 * KIB), 0, 0.60),
    ("103.42.176.201:51413", "qBittorrent v4.6.7", int(870 * KIB), int(31 * KIB), 0.64),
    ("5.9.112.83:6881", "aria2/1.37.0", int(640 * KIB), 0, 0.55),
    ("195.201.157.30:51410", "Vuze/5.7.7", int(402 * KIB), int(12 * KIB), 0.57),
    ("159.223.71.144:6887", "libtorrent/1.2.19", int(155 * KIB), 0, 0.52),
]

TRACKER_ROWS = [
    {"url": "udp://tracker.opentrackr.org:1337/announce", "seeds": 41,
     "working": True, "fails": 0, "message": ""},
    {"url": "udp://tracker.ubuntu.com:6969/announce", "seeds": 38,
     "working": True, "fails": 0, "message": ""},
    {"url": "https://torrent.ubuntu.com/announce", "seeds": 35,
     "working": True, "fails": 0, "message": ""},
    {"url": "udp://exodus.desync.com:6969/announce", "seeds": 0,
     "working": False, "fails": 3, "message": "connection timed out"},
]


def detail_of(t: dict) -> dict:
    d = {
        **t,
        "total_size": t["size"],
        "active_for": 4021,
        "next_announce": 43,
        "current_tracker": "udp://tracker.opentrackr.org:1337/announce",
        "piece_count": max(1, t["size"] // (256 * KIB)),
        "piece_size": 256 * KIB,
        "file_count": 1 if t["hash"] != ALBUM else len(TRACKS),
        "comment": "" if t["hash"] != UBUNTU else "Ubuntu CD releases.ubuntu.com",
        "creator": "mktorrent 1.1",
        "private": False,
        "error": "",
    }
    d["pieces_have"] = int(d["piece_count"] * t["progress"])
    return d


class FakeClient:
    """Stands in for ipc.AsyncClient with a deterministic demo library."""

    def __init__(self) -> None:
        self.tick = 0

    async def call(self, method: str, **params: object) -> object:
        if method == "snapshot":
            self.tick += 1
            session = {**SESSION, "dht_nodes": 380 + (self.tick * 7) % 14}
            return {"torrents": TORRENTS, "session": session}
        if method == "list":
            return TORRENTS
        if method == "get":
            return detail_of(next(t for t in TORRENTS if t["hash"] == params["hash"]))
        if method == "files":
            return [
                {"index": i, "path": name, "size": size, "progress": prog,
                 "priority_label": "normal"}
                for i, (name, size, prog) in enumerate(TRACKS)
            ]
        if method == "peers":
            return [
                {"address": a, "client": c, "down_rate": d, "up_rate": u, "progress": p}
                for a, c, d, u, p in PEERS
            ]
        if method == "trackers":
            return TRACKER_ROWS
        return True  # pause/resume/…

    async def close(self) -> None:
        pass


def sparkline_wave(peak: float, spread: float, seed: int) -> list[float]:
    """60 ticks of a plausible rate history that lands on `peak`."""
    rng = random.Random(seed)  # noqa: S311 - deterministic, not crypto
    points = [max(0.0, rng.gauss(peak * 0.72, spread)) for _ in range(59)]
    points.append(peak)
    return points


async def main() -> None:
    os.environ.setdefault("TERM", "xterm-256color")
    with tempfile.TemporaryDirectory() as home:
        os.environ["TORSH_CONFIG_DIR"] = f"{home}/config"
        os.environ["TORSH_STATE_DIR"] = f"{home}/state"
        os.environ["TORSH_CACHE_DIR"] = f"{home}/cache"

        from textual.widgets import TabbedContent

        from torsh import config as config_mod
        from torsh.tui.app import TorshApp

        cfg = config_mod.load()
        cfg.ui.show_details = True
        cfg.ui.status_filter = "all"
        cfg.ui.sort = "id"

        app = TorshApp(cfg)
        app.client = FakeClient()
        ASSETS.mkdir(parents=True, exist_ok=True)

        async def settle(frames: int = 6) -> None:
            for _ in range(frames):
                await app.refresh_data()
                await asyncio.sleep(0)

        def shoot(name: str) -> None:
            svg = app.export_screenshot(title="torsh")
            (ASSETS / name).write_text(svg, encoding="utf-8")
            print("wrote", name)

        # Idle histories so the sparklines look alive on the first frame.
        app._down_hist = sparkline_wave(SESSION["down_rate"], SESSION["down_rate"] * 0.2, seed=1)
        app._up_hist = sparkline_wave(SESSION["up_rate"], SESSION["up_rate"] * 0.25, seed=2)

        async with app.run_test(size=(WIDTH, HEIGHT)) as pilot:
            await settle()
            app.selected.clear()
            app.query_one("#torrents").cursor_coordinate = (0, 0)
            await app._refresh_details()
            await pilot.pause()
            shoot("tui-overview.svg")

            # Files tab on the multi-file album torrent.
            app.selected = {ALBUM}
            app.query_one(TabbedContent).active = "files"
            await app.refresh_data()
            await app._refresh_details()
            await pilot.pause()
            shoot("tui-files.svg")

            # Peers tab back on the downloading ISO.
            app.selected = {UBUNTU}
            app.query_one(TabbedContent).active = "peers"
            await app.refresh_data()
            await app._refresh_details()
            await pilot.pause()
            shoot("tui-peers.svg")

            # Command palette, freshly opened over the dashboard.
            app.selected.clear()
            await app.refresh_data()
            await pilot.press("ctrl+p")
            await pilot.pause()
            shoot("tui-palette.svg")


if __name__ == "__main__":
    asyncio.run(main())

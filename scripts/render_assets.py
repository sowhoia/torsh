"""Turn Textual's SVG frames into trimmed PNGs and an animated demo GIF.

qlmanage renders each SVG onto a padded square canvas, so every frame is
auto-cropped to its content first. Run after make_screenshots.py:

    .venv/bin/python scripts/render_assets.py            # PNGs + demo GIF
    .venv/bin/python scripts/render_assets.py --png-only # skip the GIF
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "docs" / "assets"
RENDER_SCALE = 2400  # qlmanage max side; generous, crops happen later
GIF_WIDTH = 1160     # README displays ~830px; this keeps text legible when scaled
PAD = 24             # breathing room around the cropped window, px


def render_svg(svg: Path, out_dir: Path) -> Image.Image:
    subprocess.run(
        ["qlmanage", "-t", "-s", str(RENDER_SCALE), "-o", str(out_dir), str(svg)],
        check=True, capture_output=True,
    )
    return Image.open(out_dir / f"{svg.name}.png")


def crop_content(img: Image.Image, background: tuple = (255, 255, 255)) -> Image.Image:
    """Trim the padded canvas down to the terminal window plus a small margin."""
    rgb = Image.new("RGB", img.size, background)
    rgb.paste(img, mask=img.split()[-1] if img.mode == "RGBA" else None)
    # Pixels that differ from the white canvas at all are content.
    bbox = Image.eval(rgb, lambda px: 255 - px if abs(px - 255) > 2 else 0).getbbox()
    if bbox:
        rgb = rgb.crop(bbox)
    return rgb


def shoot(svg_text: str, tmp: Path, index: int) -> Image.Image:
    """Render one exported SVG string to a cropped, rounded image."""
    svg_file = tmp / f"frame-{index}.svg"
    svg_file.write_text(svg_text, encoding="utf-8")
    return rounded(pad_to(crop_content(render_svg(svg_file, tmp))))


def pad_to(img: Image.Image, pad: int = PAD, color: tuple = (255, 255, 255)) -> Image.Image:
    out = Image.new("RGB", (img.width + 2 * pad, img.height + 2 * pad), color)
    out.paste(img, (pad, pad))
    return out


def rounded(img: Image.Image, radius: int = 28) -> Image.Image:
    """Round the window's corners so it sits nicely on any README background."""
    mask = Image.new("L", img.size, 0)
    from PIL import ImageDraw

    ImageDraw.Draw(mask).rounded_rectangle((0, 0, img.width - 1, img.height - 1), radius=radius, fill=255)
    out = Image.new("RGBA", img.size, (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


def png_assets(svgs: list[Path]) -> dict[str, Image.Image]:
    done: dict[str, Image.Image] = {}
    with tempfile.TemporaryDirectory() as tmp:
        for svg in svgs:
            img = pad_to(crop_content(render_svg(svg, Path(tmp))))
            img = rounded(img)
            out = ASSETS / f"{svg.stem}.png"
            img.save(out)
            print(f"{out.name}: {img.width}x{img.height}, {out.stat().st_size // 1024} KiB")
            done[svg.stem] = img
    return done


def demo_gif(frames: list[Image.Image], durations: list[int]) -> None:
    scaled = []
    for img in frames:
        ratio = GIF_WIDTH / img.width
        scaled.append(img.resize((GIF_WIDTH, round(img.height * ratio)), Image.LANCZOS))
    sizes = {(img.width, img.height) for img in scaled}
    if len(sizes) > 1:  # pad every frame to the largest canvas
        width = max(w for w, _ in sizes)
        height = max(h for _, h in sizes)
        scaled = [_on_canvas(img, width, height) for img in scaled]
    out = ASSETS / "tui-demo.gif"
    scaled[0].save(
        out, save_all=True, append_images=scaled[1:], loop=0,
        duration=durations, optimize=True,
    )
    print(f"{out.name}: {out.stat().st_size // 1024} KiB")


def _on_canvas(img: Image.Image, width: int, height: int) -> Image.Image:
    canvas = Image.new("RGBA", (width, height), (255, 255, 255, 255))
    canvas.paste(img, ((width - img.width) // 2, (height - img.height) // 2), img)
    return canvas


def main() -> None:
    svgs = sorted(ASSETS.glob("*.svg"))
    if not svgs:
        sys.exit("no SVGs in docs/assets — run make_screenshots.py first")
    png_assets(svgs)
    if "--png-only" in sys.argv:
        return

    # The animated story: dashboard → fuzzy filter narrows to one torrent →
    # command palette. Frames come straight out of make_screenshots.py's set,
    # re-shot here against the same FakeClient so the GIF is reproducible.
    import asyncio

    from make_screenshots import HEIGHT, WIDTH, FakeClient

    with tempfile.TemporaryDirectory() as home:
        import os

        os.environ.update(
            TORSH_CONFIG_DIR=f"{home}/config", TORSH_STATE_DIR=f"{home}/state", TORSH_CACHE_DIR=f"{home}/cache",
        )
        from textual.widgets import Input

        from torsh import config as config_mod
        from torsh.tui.app import TorshApp

        async def shoot_frames() -> list[Image.Image]:
            cfg = config_mod.load()
            cfg.ui.show_details = True
            cfg.ui.sort = "id"
            app = TorshApp(cfg)
            app.client = FakeClient()
            frames: list[Image.Image] = []
            with tempfile.TemporaryDirectory() as tmp:
                async with app.run_test(size=(WIDTH, HEIGHT)) as pilot:
                    for _ in range(6):
                        await app.refresh_data()
                        await asyncio.sleep(0)
                    await pilot.pause()
                    frames.append(shoot(app.export_screenshot(title="torsh"), Path(tmp), 0))
                    # Set the filter value directly: pilot char presses are
                    # delivered twice to the Input, which would type "ububu".
                    app.action_focus_filter()
                    finput = app.query_one("#filter-input", Input)
                    for i, text in enumerate(("u", "ub", "ubunt"), start=1):
                        finput.value = text
                        await pilot.pause()
                        frames.append(shoot(app.export_screenshot(title="torsh"), Path(tmp), i))
                    finput.value = ""
                    app.action_clear_ui()
                    await pilot.pause()
                    await pilot.press("ctrl+p")
                    await pilot.pause()
                    frames.append(shoot(app.export_screenshot(title="torsh"), Path(tmp), 5))
            return frames

        frames = asyncio.run(shoot_frames())
        demo_gif(frames, durations=[1400, 500, 500, 900, 700, 1400])


if __name__ == "__main__":
    main()

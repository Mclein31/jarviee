"""Generate macOS .icns app icon matching Jarviee design tokens."""
import shutil
import subprocess
import tempfile
from pathlib import Path


def _pil():
    from PIL import Image, ImageDraw
    return Image, ImageDraw


def create_master_image(size=1024):
    """Render a 1024x1024 master icon matching Jarviee's data-terminal tokens."""
    Image, ImageDraw = _pil()
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Outer squircle / rounded rect
    margin = int(size * 0.08)
    radius = int(size * 0.22)
    outer_box = [margin, margin, size - margin, size - margin]
    draw.rounded_rectangle(outer_box, radius=radius, fill="#171719", outline="#2a292e", width=int(size * 0.015))

    # Inner raised terminal plate
    inner_margin = int(size * 0.15)
    inner_radius = int(size * 0.16)
    inner_box = [inner_margin, inner_margin, size - inner_margin, size - inner_margin]
    draw.rounded_rectangle(inner_box, radius=inner_radius, fill="#1c1b20", outline="#212023", width=int(size * 0.01))

    # Center geometric hub glyph: command tiers / gateway chevron
    mid_x, mid_y = size // 2, size // 2
    accent = "#4a8ad6"   # primary accent blue
    pos = "#5fc39c"      # positive status green
    attn = "#d9a441"     # amber status

    # Top chevron / prompt prompt: >_
    stroke_w = int(size * 0.035)
    # Tier 1 (top bar) - blue accent
    bar1_y = mid_y - int(size * 0.16)
    draw.rounded_rectangle([mid_x - int(size * 0.22), bar1_y, mid_x + int(size * 0.22), bar1_y + stroke_w],
                           radius=stroke_w // 2, fill=accent)

    # Tier 2 (middle bar) - green pos
    bar2_y = mid_y - int(size * 0.04)
    draw.rounded_rectangle([mid_x - int(size * 0.22), bar2_y, mid_x + int(size * 0.14), bar2_y + stroke_w],
                           radius=stroke_w // 2, fill=pos)

    # Tier 3 (lower bar) - white/text
    bar3_y = mid_y + int(size * 0.08)
    draw.rounded_rectangle([mid_x - int(size * 0.22), bar3_y, mid_x + int(size * 0.06), bar3_y + stroke_w],
                           radius=stroke_w // 2, fill="#f2f2f3")

    # Amber attention dot (indicator)
    dot_r = int(size * 0.035)
    dot_x = mid_x + int(size * 0.18)
    dot_y = bar3_y + stroke_w // 2
    draw.ellipse([dot_x - dot_r, dot_y - dot_r, dot_x + dot_r, dot_y + dot_r], fill=attn)

    return img


def master_from_svg(size=1024):
    """The Jarviee mark (hub/static/mark.svg) rendered by QuickLook, or None (then the drawn fallback is used)."""
    Image, _ = _pil()
    src = Path(__file__).with_name("static") / "mark.svg"
    with tempfile.TemporaryDirectory() as d:
        try:
            subprocess.run(["/usr/bin/qlmanage", "-t", "-s", str(size), "-o", d, str(src)], capture_output=True, timeout=60)
            out = Path(d) / (src.name + ".png")
            return Image.open(out).convert("RGBA").copy() if out.is_file() else None
        except (OSError, subprocess.SubprocessError):
            return None


def build_icns(dest_path):
    """Generate .icns file using iconutil."""
    Image, _ = _pil()
    dest = Path(dest_path).resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)
    mark = master_from_svg(1024)
    if mark is not None:   # macOS icon grid: the tile sits at about 824 of 1024 px, like every other app's
        master = Image.new("RGBA", (1024, 1024), (0, 0, 0, 0))
        master.alpha_composite(mark.resize((824, 824), Image.Resampling.LANCZOS), (100, 100))
    else:
        master = create_master_image(1024)

    sizes = [
        ("icon_16x16.png", 16),
        ("icon_16x16@2x.png", 32),
        ("icon_32x32.png", 32),
        ("icon_32x32@2x.png", 64),
        ("icon_128x128.png", 128),
        ("icon_128x128@2x.png", 256),
        ("icon_256x256.png", 256),
        ("icon_256x256@2x.png", 512),
        ("icon_512x512.png", 512),
        ("icon_512x512@2x.png", 1024),
    ]

    with tempfile.TemporaryDirectory() as tmpdir:
        iconset = Path(tmpdir) / "Jarviee.iconset"
        iconset.mkdir()
        for filename, s in sizes:
            resized = master.resize((s, s), Image.Resampling.LANCZOS)
            resized.save(iconset / filename, "PNG")

        cmd = ["/usr/bin/iconutil", "-c", "icns", str(iconset), "-o", str(dest)]
        subprocess.run(cmd, check=True, capture_output=True)
    return dest

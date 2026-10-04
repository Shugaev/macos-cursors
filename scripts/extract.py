#!/usr/bin/env python3
"""Extract the macOS system cursors from the installed OS into SVG, PNG and JSON.

The cursors ship with macOS as vector PDFs. This script converts them without
retracing, so the SVGs keep Apple's exact geometry. Run it on a Mac:

    python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
    .venv/bin/python scripts/extract.py
"""

import json
import os
import platform
import plistlib
import re
import shutil
import subprocess
import sys

import fitz  # PyMuPDF

from shading import ShadingCollector

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HISERVICES = ("/System/Library/Frameworks/ApplicationServices.framework/Versions/A/Frameworks/"
              "HIServices.framework/Versions/A/Resources/cursors")
ACCESSIBILITY = ("/System/Library/PrivateFrameworks/AccessibilitySupport.framework/Versions/A/Frameworks/"
                 "AccessibilityFoundation.framework/Versions/A/Resources/Cursors-macOS16")
# Newer artwork that replaces the HIServices cursor of the same name.
OVERRIDES = "macos27"
PNG_SCALES = (1, 2, 4)
# The arrow is not in HIServices; the Accessibility copy is split into an outline
# and a fill layer. Its tip sits at the same point as the arrow inside "copy".
ARROW_HOTSPOT = (5.0, 5.0)


def main():
    out = {kind: os.path.join(ROOT, kind) for kind in ("svg", "png")}
    for path in out.values():
        shutil.rmtree(path, ignore_errors=True)
        os.makedirs(path)

    cursors = []
    for name, folder in cursor_folders():
        info = plistlib.load(open(os.path.join(folder, "info.plist"), "rb"))
        doc = fitz.open(os.path.join(folder, "cursor.pdf"))
        cursors.append(export_cursor(name, doc, info, out))
    cursors.append(export_arrow(out))
    cursors.sort(key=lambda cursor: cursor["name"])

    manifest = {"macos": macos_version(), "cursors": cursors}
    with open(os.path.join(ROOT, "cursors.json"), "w") as file:
        json.dump(manifest, file, indent=1)
        file.write("\n")
    write_preview(cursors, out["png"])
    print(f"Exported {len(cursors)} cursors from macOS {manifest['macos']}")


def cursor_folders():
    folders = {}
    for name in sorted(os.listdir(HISERVICES)):
        path = os.path.join(HISERVICES, name)
        if name != OVERRIDES and os.path.isfile(os.path.join(path, "cursor.pdf")):
            folders[name] = path
    override_root = os.path.join(HISERVICES, OVERRIDES)
    if os.path.isdir(override_root):
        for name in sorted(os.listdir(override_root)):
            path = os.path.join(override_root, name)
            if os.path.isfile(os.path.join(path, "cursor.pdf")):
                folders[name] = path
    return sorted(folders.items())


def export_cursor(name, doc, info, out):
    page = doc[0]
    frames = int(info.get("frames") or 1)
    width, height = page.rect.width, page.rect.height / frames
    svg = page_svg(doc, page)
    hotspot = [float(info.get("hotx", width / 2)), float(info.get("hoty", height / 2))]

    if frames == 1:
        write_svg(os.path.join(out["svg"], f"{name}.svg"), svg, width, height)
        write_pngs(page, fitz.Rect(0, 0, width, height), out["png"], name)
    else:
        # Frames are stacked top to bottom on one page.
        for index in range(frames):
            frame = f"{name}-{index + 1:02d}"
            write_svg(os.path.join(out["svg"], f"{frame}.svg"), svg, width, height, y=index * height)
            write_pngs(page, fitz.Rect(0, index * height, width, (index + 1) * height), out["png"], frame)

    cursor = {"name": name, "width": tidy(width), "height": tidy(height), "hotspot": [tidy(v) for v in hotspot]}
    if frames > 1:
        cursor["frames"] = frames
        cursor["frameDuration"] = tidy(float(info.get("delay") or 0))
    return cursor


def tidy(value):
    value = round(value, 4)
    return int(value) if value == int(value) else value


def export_arrow(out):
    outline = fitz.open(os.path.join(ACCESSIBILITY, "arrow-top.pdf"))
    fill = fitz.open(os.path.join(ACCESSIBILITY, "arrow-bottom.pdf"))
    width, height = outline[0].rect.width, outline[0].rect.height
    outline_svg = page_svg(outline, outline[0]).replace("clip_", "outline_clip_")
    fill_svg = page_svg(fill, fill[0])
    svg = (f'<g fill="#fff">{svg_content(outline_svg)}</g>'
           f'<g fill="#000">{svg_content(fill_svg)}</g>')
    write_svg(os.path.join(out["svg"], "arrow.svg"), svg, width, height, wrapped=True)

    # Render the PNGs from the two PDFs so they stay pixel-exact as well.
    for scale in PNG_SCALES:
        matrix = fitz.Matrix(scale, scale)
        base = outline[0].get_pixmap(matrix=matrix, alpha=True)
        outline_mask = alpha_values(base)
        fill_mask = alpha_values(fill[0].get_pixmap(matrix=matrix, alpha=True))
        samples = bytearray(len(outline_mask) * 4)
        for index, (white, black) in enumerate(zip(outline_mask, fill_mask)):
            # White outline under the black fill, composited per pixel.
            alpha = black + white * (255 - black) // 255
            # MuPDF pixmaps store premultiplied color.
            gray = white * (255 - black) // 255
            samples[index * 4:index * 4 + 4] = bytes((gray, gray, gray, alpha))
        pixmap = fitz.Pixmap(fitz.csRGB, base.width, base.height, bytes(samples), True)
        pixmap.save(png_path(out["png"], "arrow", scale))
    return {"name": "arrow", "width": tidy(width), "height": tidy(height), "hotspot": [tidy(v) for v in ARROW_HOTSPOT]}


def alpha_values(pixmap):
    samples = pixmap.samples
    return [samples[index] for index in range(pixmap.n - 1, len(samples), pixmap.n)]


def page_svg(doc, page):
    """PyMuPDF SVG with every shading rebuilt as a vector gradient instead of a bitmap."""
    svg = page.get_svg_image(text_as_path=True)
    svg = re.sub(r'\s*xmlns:inkscape="[^"]*"|\s*inkscape:[\w-]+="[^"]*"', "", svg)
    shadings = list(ShadingCollector(doc, page).shadings)
    images = re.findall(r"<image\b.*?/>", svg, flags=re.S)
    if len(images) != len(shadings):
        raise SystemExit(f"{doc.name}: {len(images)} bitmaps but {len(shadings)} shadings")
    defs = []
    for index, (image, shading) in enumerate(zip(images, shadings)):
        attrs = dict(re.findall(r'(\w+)="([^"]*)"', image.split("xlink:href")[0]))
        gradient_id = f"shading_{index + 1}"
        defs.append(gradient_svg(gradient_id, shading, page.rect.height))
        style = ""
        if shading["blend"] != "Normal":
            style = f' style="mix-blend-mode:{css_blend(shading["blend"])}"'
        opacity = f' opacity="{shading["alpha"]:g}"' if shading["alpha"] < 1 else ""
        rect = (f'<rect x="{attrs["x"]}" y="{attrs["y"]}" width="{attrs["width"]}" '
                f'height="{attrs["height"]}" fill="url(#{gradient_id})"{opacity}{style}/>')
        svg = svg.replace(image, rect, 1)
    if defs:
        gradients = "".join(defs)
        if "<defs>" in svg:
            svg = svg.replace("<defs>", "<defs>" + gradients, 1)
        else:
            svg = re.sub(r"(<svg[^>]*>)", r"\1<defs>" + gradients + "</defs>", svg, count=1)
    return svg


def svg_content(svg):
    inner = re.sub(r"^.*?<svg[^>]*>", "", svg, flags=re.S)
    return inner[: inner.rfind("</svg>")].strip()


def write_svg(path, svg, width, height, y=0, wrapped=False):
    content = svg if wrapped else svg_content(svg)
    with open(path, "w") as file:
        file.write(
            '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
            f'width="{width:g}" height="{height:g}" viewBox="0 {y:g} {width:g} {height:g}">'
            f"{content}</svg>\n"
        )


def write_pngs(page, clip, folder, name):
    for scale in PNG_SCALES:
        pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=clip, alpha=True)
        pixmap.save(png_path(folder, name, scale))


def png_path(folder, name, scale):
    return os.path.join(folder, f"{name}.png" if scale == 1 else f"{name}@{scale}x.png")


def gradient_svg(gradient_id, shading, page_height):
    a, b, c, d, e, f = shading["matrix"]
    # PDF user space has y pointing up; the SVG from PyMuPDF has it pointing down.
    transform = f"matrix({a:g} {-b:g} {c:g} {-d:g} {e:g} {page_height - f:g})"
    stops = "".join(
        f'<stop offset="{offset:g}" stop-color="{hex_color(color)}"/>' for offset, color in shading["stops"]
    )
    x1, y1, x2, y2 = shading["coords"]
    return (f'<linearGradient id="{gradient_id}" gradientUnits="userSpaceOnUse" '
            f'x1="{x1:g}" y1="{y1:g}" x2="{x2:g}" y2="{y2:g}" gradientTransform="{transform}">{stops}</linearGradient>')


def hex_color(color):
    if len(color) == 1:
        color = color * 3
    return "#" + "".join(f"{round(max(0, min(1, value)) * 255):02X}" for value in color)


def css_blend(mode):
    return re.sub(r"(?<!^)(?=[A-Z])", "-", mode).lower()


def macos_version():
    try:
        version = subprocess.check_output(["sw_vers", "-productVersion"], text=True).strip()
        build = subprocess.check_output(["sw_vers", "-buildVersion"], text=True).strip()
        return f"{version} ({build})"
    except OSError:
        return platform.mac_ver()[0]


def write_preview(cursors, png_folder):
    cell, columns, label = 96, 8, 18
    rows = (len(cursors) + columns - 1) // columns
    doc = fitz.open()
    page = doc.new_page(width=cell * columns, height=(cell + label) * rows)
    page.draw_rect(page.rect, color=None, fill=(0.93, 0.94, 0.96))
    for index, cursor in enumerate(cursors):
        x, y = (index % columns) * cell, (index // columns) * (cell + label)
        name = cursor["name"] + ("-01" if cursor.get("frames") else "")
        width, height = 2 * cursor["width"], 2 * cursor["height"]
        box = fitz.Rect(x + (cell - width) / 2, y + (cell - height) / 2, x + (cell + width) / 2, y + (cell + height) / 2)
        page.insert_image(box, filename=os.path.join(png_folder, f"{name}@4x.png"))
        page.insert_textbox(fitz.Rect(x, y + cell - 6, x + cell, y + cell + label), cursor["name"],
                            fontsize=7.5, align=1, color=(0.3, 0.3, 0.35))
    page.get_pixmap(matrix=fitz.Matrix(2, 2)).save(os.path.join(ROOT, "preview.png"))


if __name__ == "__main__":
    if sys.platform != "darwin":
        raise SystemExit("Run this on macOS: the cursors are read from the installed system.")
    main()

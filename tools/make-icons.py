#!/usr/bin/env python3
"""Render web/icons/*.svg to PNG with Playwright (run with tests/.venv/bin/python)."""
import pathlib
from playwright.sync_api import sync_playwright
root = pathlib.Path(__file__).resolve().parent.parent / "web" / "icons"
with sync_playwright() as p:
    b = p.chromium.launch(); pg = b.new_page()
    for svg, name, size in [("icon.svg", "icon-192.png", 192), ("icon.svg", "icon-512.png", 512), ("icon-maskable.svg", "icon-maskable-512.png", 512)]:
        pg.set_viewport_size({"width": size, "height": size})
        pg.set_content(f"<body style='margin:0'><img src='data:image/svg+xml;base64,{__import__('base64').b64encode((root/svg).read_bytes()).decode()}' width={size} height={size}>")
        pg.screenshot(path=str(root / name), omit_background=True)
    b.close()

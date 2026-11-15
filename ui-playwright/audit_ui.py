#!/usr/bin/env python3
"""Deterministic UI audit: device matrix x languages -> JSON report + screenshots.

Checks per cell: horizontal scroll, elements past the right edge, overlapping
top-level blocks (getBoundingClientRect), CLS, console errors, 4xx/5xx responses,
<html lang>, loaded font families. Exit code 1 if any cell has issues.

Usage:
  python audit_ui.py --url https://example.com --langs /,/ua/,/ru/
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

from playwright.sync_api import sync_playwright

DEFAULT_DEVICES = [
    "iPhone SE", "iPhone 14", "iPhone 14 Pro Max", "Pixel 7", "Galaxy S9+",
    "iPad Mini", "iPad Pro 11 landscape", "Desktop Chrome",
]
CUSTOM_VIEWPORTS = {
    "Laptop 1024x700": {"viewport": {"width": 1024, "height": 700}},
    "Desktop 1440x900 DPR2": {"viewport": {"width": 1440, "height": 900}, "device_scale_factor": 2},
}

# Buffered CLS observer, installed before any page script runs.
CLS_INIT = """
window.__cls = 0;
try {
  new PerformanceObserver((list) => {
    for (const e of list.getEntries()) if (!e.hadRecentInput) window.__cls += e.value;
  }).observe({type: 'layout-shift', buffered: true});
} catch (e) {}
"""

PROBE_JS = """
() => {
  const doc = document.documentElement;
  const vw = doc.clientWidth;
  const hscroll = doc.scrollWidth > vw + 1;
  const offRight = [];
  for (const el of document.body.querySelectorAll('*')) {
    const cs = getComputedStyle(el);
    if (cs.position === 'fixed' || cs.visibility === 'hidden' || cs.display === 'none') continue;
    const r = el.getBoundingClientRect();
    if (r.width > 0 && r.right > vw + 1 && offRight.length < 10) {
      offRight.push((el.tagName + (el.id ? '#' + el.id : '') +
        (el.className && typeof el.className === 'string' ? '.' + el.className.trim().split(/\\s+/)[0] : '')) +
        ' right=' + Math.round(r.right));
    }
  }
  // Top-level blocks: direct children of body/main that are visible sections.
  const blocks = [...document.querySelectorAll('body > header, body > footer, main > *, body > section')]
    .filter(el => { const r = el.getBoundingClientRect(); return r.height > 4 && r.width > 4; })
    .filter(el => !el.matches('canvas, [aria-hidden="true"], [data-qa-ignore]'));
  const overlaps = [];
  const abs = el => { const r = el.getBoundingClientRect();
    return {t: r.top + scrollY, b: r.bottom + scrollY, l: r.left, r: r.right}; };
  const name = el => el.tagName.toLowerCase() + (el.id ? '#' + el.id : '');
  for (let i = 0; i < blocks.length; i++) for (let j = i + 1; j < blocks.length; j++) {
    const a = abs(blocks[i]), b = abs(blocks[j]);
    const v = Math.min(a.b, b.b) - Math.max(a.t, b.t);
    const h = Math.min(a.r, b.r) - Math.max(a.l, b.l);
    const pa = getComputedStyle(blocks[i]).position, pb = getComputedStyle(blocks[j]).position;
    if (v > 2 && h > 2 && !['fixed', 'sticky'].includes(pa) && !['fixed', 'sticky'].includes(pb))
      overlaps.push(name(blocks[i]) + ' x ' + name(blocks[j]) + ' (' + Math.round(v) + 'px)');
  }
  const fonts = [...new Set([...document.fonts].filter(f => f.status === 'loaded').map(f => f.family.replace(/"/g, '')))];
  return {hscroll, scrollWidth: doc.scrollWidth, viewport: vw, offRight, overlaps,
          cls: Math.round((window.__cls || 0) * 1000) / 1000, lang: doc.lang || null, fonts,
          scrollHeight: doc.scrollHeight};
}
"""


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "root"


def audit_cell(browser, device_name, ctx_opts, url, shots_dir):
    ctx = browser.new_context(**ctx_opts)
    ctx.add_init_script(CLS_INIT)
    page = ctx.new_page()
    console_errors, bad_responses = [], []
    page.on("console", lambda m: console_errors.append(m.text[:200]) if m.type == "error" else None)
    page.on("response", lambda r: bad_responses.append(f"{r.status} {r.url[:150]}") if r.status >= 400 else None)
    cell = {"device": device_name, "url": url, "issues": []}
    try:
        page.goto(url, wait_until="networkidle", timeout=45000)
        top = page.evaluate(PROBE_JS)
        # Scroll to the bottom in steps (catches lazy blocks, layout jumps, scroll-back bugs).
        last_y, scroll_back = 0, 0
        for _ in range(60):
            page.mouse.wheel(0, 600)
            page.wait_for_timeout(120)
            y = page.evaluate("scrollY")
            if y < last_y - 1:
                scroll_back += 1
            if y == last_y:
                break
            last_y = y
        bottom = page.evaluate(PROBE_JS)
        slug_name = f"{slug(device_name)}__{slug(url.split('//', 1)[-1])}"
        page.screenshot(path=str(shots_dir / f"{slug_name}.png"), full_page=True)
        cell.update(top=top, bottom=bottom, console_errors=console_errors[:10],
                    bad_responses=bad_responses[:10], scroll_back_events=scroll_back,
                    screenshot=f"{slug_name}.png")
        for probe in (top, bottom):
            if probe["hscroll"]:
                cell["issues"].append(f"horizontal scroll: {probe['scrollWidth']} > {probe['viewport']}")
            cell["issues"] += [f"off right edge: {x}" for x in probe["offRight"]]
            cell["issues"] += [f"overlap: {x}" for x in probe["overlaps"]]
        if bottom["cls"] >= 0.1:
            cell["issues"].append(f"CLS {bottom['cls']} >= 0.1")
        if scroll_back:
            cell["issues"].append(f"scrollY jumped back {scroll_back}x while scrolling down")
        if len(bottom["fonts"]) > 3:
            cell["issues"].append(f"many font families loaded: {bottom['fonts']}")
        cell["issues"] += [f"console error: {e}" for e in console_errors[:5]]
        cell["issues"] += [f"HTTP {r}" for r in bad_responses[:5]]
        cell["issues"] = sorted(set(cell["issues"]))
    except Exception as e:  # report, don't crash the whole matrix
        cell["issues"].append(f"audit failed: {type(e).__name__}: {str(e)[:200]}")
    finally:
        ctx.close()
    return cell


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True, help="site root, e.g. https://example.com")
    ap.add_argument("--langs", default="/", help="comma-separated paths, e.g. /,/ua/,/ru/")
    ap.add_argument("--devices", default=",".join(DEFAULT_DEVICES + list(CUSTOM_VIEWPORTS)),
                    help="comma-separated Playwright device names or custom viewport names")
    ap.add_argument("--out", default="reports", help="output folder")
    args = ap.parse_args()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out = Path(args.out) / f"ui-{stamp}"
    shots = out / "screenshots"
    shots.mkdir(parents=True, exist_ok=True)
    urls = [urljoin(args.url.rstrip("/") + "/", p.lstrip("/")) for p in args.langs.split(",")]

    cells = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for name in [d.strip() for d in args.devices.split(",") if d.strip()]:
            if name in CUSTOM_VIEWPORTS:
                opts = dict(CUSTOM_VIEWPORTS[name])
            elif name in p.devices:
                opts = {k: v for k, v in p.devices[name].items() if k != "default_browser_type"}
            else:
                print(f"unknown device: {name}", file=sys.stderr)
                continue
            for url in urls:
                cell = audit_cell(browser, name, opts, url, shots)
                cells.append(cell)
                mark = "OK " if not cell["issues"] else f"{len(cell['issues'])} issue(s)"
                print(f"[{mark:>11}] {name:<24} {url}")
        browser.close()

    report = {"tool": "ai-web-qa-kit/audit_ui", "generated_at": stamp, "url": args.url,
              "cells": cells, "failed_cells": sum(1 for c in cells if c["issues"])}
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\nReport: {out / 'report.json'}  ({report['failed_cells']}/{len(cells)} cells with issues)")
    return 1 if report["failed_cells"] else 0


if __name__ == "__main__":
    sys.exit(main())

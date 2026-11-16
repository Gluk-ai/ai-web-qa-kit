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

# Layout-shift recorder, installed before any page script runs. CLS is computed the
# web.dev way: shifts grouped into session windows (gap < 1 s, window <= 5 s); the score
# is the largest window. Chromium only (Layout Instability API).
# hadRecentInput is deliberately NOT used: with mobile device emulation (has_touch)
# Chromium marks early shifts as "after input" (lastInputTime ~15 ms, no real input),
# which would hide every load shift on phones. This audit sends no clicks or keys, only
# wheel scrolling, which the spec never counts as input - so every shift is unexpected.
CLS_INIT = """
window.__ls = [];
try {
  new PerformanceObserver((list) => {
    for (const e of list.getEntries())
      window.__ls.push({t: e.startTime, v: e.value,
        src: (e.sources || []).map(s => s.node && s.node.nodeName ? s.node.nodeName.toLowerCase() +
             (s.node.id ? '#' + s.node.id : '') : null).filter(Boolean).slice(0, 3)});
  }).observe({type: 'layout-shift', buffered: true});
} catch (e) {}
"""

CLS_JS = """
(since) => {
  const es = (window.__ls || []).filter(e => e.t >= since);
  let best = 0, bestSrc = [], cur = 0, curSrc = [], start = -1, last = -1;
  for (const e of es) {
    if (start < 0 || e.t - last >= 1000 || e.t - start > 5000) { cur = 0; curSrc = []; start = e.t; }
    cur += e.v; curSrc = curSrc.concat(e.src); last = e.t;
    if (cur > best) { best = cur; bestSrc = curSrc.slice(0, 3); }
  }
  return {cls: Math.round(best * 1000) / 1000, sources: [...new Set(bestSrc)]};
}
"""

PROBE_JS = """
() => {
  const doc = document.documentElement;
  const vw = doc.clientWidth;
  const hscroll = doc.scrollWidth > vw + 1;
  const offRight = [];
  // An element past the edge is harmless if an ancestor clips it inside the viewport
  // (overflow hidden/clip) or holds it in a scroll container (carousels, overflow auto/scroll). Only unclipped overflow counts.
  const clipped = (el) => {
    for (let a = el.parentElement; a && a !== document.documentElement; a = a.parentElement) {
      const cs = getComputedStyle(a);
      if (['hidden', 'clip', 'auto', 'scroll'].includes(cs.overflowX) && a.getBoundingClientRect().right <= vw + 1) return true;
    }
    return false;
  };
  for (const el of document.body.querySelectorAll('*')) {
    const cs = getComputedStyle(el);
    if (cs.position === 'fixed' || cs.visibility === 'hidden' || cs.display === 'none') continue;
    const r = el.getBoundingClientRect();
    if (r.width > 0 && r.right > vw + 1 && offRight.length < 10 && !clipped(el)) {
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
          lang: doc.lang || null, fonts,
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
        cls_load = page.evaluate(CLS_JS, 0)
        scroll_start = page.evaluate("performance.now()")
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
        cls_scroll = page.evaluate(CLS_JS, scroll_start)
        slug_name = f"{slug(device_name)}__{slug(url.split('//', 1)[-1])}"
        page.screenshot(path=str(shots_dir / f"{slug_name}.png"), full_page=True)
        cell.update(top=top, bottom=bottom, cls_load=cls_load, cls_scroll=cls_scroll, console_errors=console_errors[:10],
                    bad_responses=bad_responses[:10], scroll_back_events=scroll_back,
                    screenshot=f"{slug_name}.png")
        for probe in (top, bottom):
            if probe["hscroll"]:
                cell["issues"].append(f"horizontal scroll: {probe['scrollWidth']} > {probe['viewport']}")
            cell["issues"] += [f"off right edge: {x}" for x in probe["offRight"]]
            cell["issues"] += [f"overlap: {x}" for x in probe["overlaps"]]
        for phase, c in (("load", cls_load), ("scroll", cls_scroll)):
            if c["cls"] >= 0.1:  # lab estimate from a single run; confirm with PageSpeed field data
                cell["issues"].append(f"CLS ({phase}) {c['cls']} >= 0.1, shifted: {', '.join(c['sources']) or '?'}")
        if scroll_back:
            cell["issues"].append(f"scrollY jumped back {scroll_back}x while scrolling down")
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

    # Same device, different language -> the loaded font families must match
    # (a missing Cyrillic/Greek subset silently falls back to a system font).
    by_device: dict[str, list] = {}
    for c in cells:
        if "bottom" in c:
            by_device.setdefault(c["device"], []).append(c)
    for dev, group in by_device.items():
        base = set(group[0]["bottom"]["fonts"])
        for c in group[1:]:
            diff = set(c["bottom"]["fonts"]) ^ base
            if diff:
                c["issues"].append(f"font families differ from {group[0]['url']}: {sorted(diff)}")
                print(f"[font issue ] {dev:<24} {c['url']}  {sorted(diff)}")

    report = {"tool": "ai-web-qa-kit/audit_ui", "generated_at": stamp, "url": args.url,
              "cells": cells, "failed_cells": sum(1 for c in cells if c["issues"])}
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\nReport: {out / 'report.json'}  ({report['failed_cells']}/{len(cells)} cells with issues)")
    return 1 if report["failed_cells"] else 0


if __name__ == "__main__":
    sys.exit(main())

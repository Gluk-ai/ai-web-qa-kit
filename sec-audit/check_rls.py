#!/usr/bin/env python3
"""Supabase RLS probe: act as an anonymous visitor and see what the anon key can do.

Read probe (default): GET each table with the anon key. Rows returned = anyone on
the internet can read them. Fine for public content, critical for leads/users.
Write probe (--write-probe, opt-in): tries to INSERT an empty row. Only run it on
your own project, ideally staging. A 201 means anonymous users can write.

Also decodes the key's JWT `role` claim: a service_role key must never ship to a browser.

Usage:
  SUPABASE_URL=https://xyz.supabase.co SUPABASE_ANON_KEY=... python check_rls.py
  python check_rls.py --tables leads,profiles --write-probe
Exit code 1 if anything critical is found.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request

SENSITIVE_HINTS = ("lead", "user", "profile", "customer", "client", "order", "message",
                   "contact", "payment", "invoice", "secret", "token", "admin")


def jwt_role(key: str) -> str | None:
    try:
        payload = key.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get("role")
    except Exception:
        return None  # new-style publishable keys (sb_publishable_...) are not JWTs


def request(method: str, url: str, key: str, body: bytes | None = None, extra: dict | None = None):
    headers = {"apikey": key, "Authorization": f"Bearer {key}", "Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    headers.update(extra or {})
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except urllib.error.URLError as e:
        return 0, str(e.reason)


def discover_tables(base: str, key: str) -> list[str]:
    status, text = request("GET", f"{base}/rest/v1/", key)
    if status != 200:
        return []
    try:
        paths = json.loads(text).get("paths", {})
    except json.JSONDecodeError:
        return []
    return sorted(p.strip("/") for p in paths if p.strip("/") and not p.startswith("/rpc/"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=os.getenv("SUPABASE_URL"))
    ap.add_argument("--key", default=os.getenv("SUPABASE_ANON_KEY"), help="anon/publishable key")
    ap.add_argument("--tables", help="comma-separated; default: discover via the REST schema")
    ap.add_argument("--write-probe", action="store_true", help="try anonymous INSERT (own projects only)")
    ap.add_argument("--json", help="write findings to this file")
    args = ap.parse_args()
    if not args.url or not args.key:
        ap.error("set --url/--key or SUPABASE_URL/SUPABASE_ANON_KEY")

    base = args.url.rstrip("/")
    findings: list[dict] = []

    def add(sev: str, table: str, msg: str):
        findings.append({"severity": sev, "table": table, "finding": msg})
        print(f"[{sev:>8}] {table:<24} {msg}")

    role = jwt_role(args.key)
    if role == "service_role":
        add("CRITICAL", "-", "this is a service_role key: bypasses RLS, must never be in frontend code or dist/")

    tables = [t.strip() for t in args.tables.split(",")] if args.tables else discover_tables(base, args.key)
    if not tables:
        add("info", "-", "no tables found (schema hidden from anon, or none exposed) - pass --tables explicitly")

    for t in tables:
        status, text = request("GET", f"{base}/rest/v1/{t}?select=*&limit=3", args.key)
        if status == 200:
            try:
                rows = json.loads(text)
            except json.JSONDecodeError:
                rows = []
            if rows:
                sev = "CRITICAL" if any(h in t.lower() for h in SENSITIVE_HINTS) else "warn"
                cols = sorted(rows[0].keys()) if isinstance(rows[0], dict) else []
                add(sev, t, f"anon can READ rows (columns: {', '.join(cols[:8])})")
            else:
                add("ok", t, "read returns 0 rows (RLS filters, or table empty - check with data)")
        elif status in (401, 403, 404):
            add("ok", t, f"read blocked ({status})")
        else:
            add("warn", t, f"read returned {status}: {text[:120]}")

        if args.write_probe:
            status, text = request("POST", f"{base}/rest/v1/{t}", args.key, b"{}",
                                   {"Prefer": "return=minimal"})
            if status in (200, 201, 204):
                add("CRITICAL", t, "anon can INSERT - a test row was created, delete it")
            elif status in (401, 403) or "42501" in text or "row-level security" in text:
                add("ok", t, f"insert blocked ({status})")
            else:
                add("check", t, f"insert returned {status} (not an RLS denial - review manually): {text[:120]}")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({"url": base, "key_role": role, "findings": findings}, f, ensure_ascii=False, indent=2)
    return 1 if any(f["severity"] == "CRITICAL" for f in findings) else 0


if __name__ == "__main__":
    sys.exit(main())

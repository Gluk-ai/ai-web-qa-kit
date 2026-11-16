#!/usr/bin/env python3
"""Supabase RLS probe: act as an anonymous visitor and see what the anon key can do.

Read probe (default): GET each table with the anon key. Rows returned = anyone on
the internet can read them. Fine for public content, critical for leads/users.
Write probe (--write-probe TABLE, opt-in, own projects only - ideally staging): needs the
service_role key in SUPABASE_SERVICE_ROLE_KEY (env only, never in code). The script creates
its OWN probe row with the service key, then the anon key tries to UPDATE and DELETE exactly
that row and to INSERT a new one. Results are verified with the service key (a 200 with no
error is not proof - RLS often "succeeds" while changing 0 rows). All probe rows are removed
at the end; live rows are never touched. If the service key can't see the probe row, the
result is "unclear", not "protected".

Also decodes the key's JWT `role` claim: a service_role key must never ship to a browser.

Usage:
  SUPABASE_URL=https://xyz.supabase.co SUPABASE_ANON_KEY=... python check_rls.py
  SUPABASE_SERVICE_ROLE_KEY=... python check_rls.py --write-probe leads \
      --probe-row '{"name":"qa-probe"}' --probe-update '{"name":"qa-probe-changed"}' 
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


def write_probe(base: str, args, add) -> None:
    t, pk = args.write_probe, args.pk
    svc = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not svc:
        add("check", t, "write probe skipped: set SUPABASE_SERVICE_ROLE_KEY (needed to verify and clean up)")
        return
    rep = {"Prefer": "return=representation"}
    row = json.loads(args.probe_row)
    status, text = request("POST", f"{base}/rest/v1/{t}", svc, json.dumps(row).encode(), rep)
    if status not in (200, 201):
        add("check", t, f"could not create probe row with service key ({status}): {text[:120]}")
        return
    created = json.loads(text)[0]
    rid = created.get(pk)
    flt = f"{pk}=eq.{rid}"
    probe_ids = [rid]
    try:
        # UPDATE by anon, verified via service key
        patch = json.loads(args.probe_update) if args.probe_update else None
        if patch:
            request("PATCH", f"{base}/rest/v1/{t}?{flt}", args.key, json.dumps(patch).encode())
            st, after = request("GET", f"{base}/rest/v1/{t}?{flt}&select=*", svc)
            rows = json.loads(after) if st == 200 else []
            if not rows:
                add("check", t, "probe row not visible to service key after UPDATE - unclear")
            elif all(rows[0].get(k) == created.get(k) for k in patch):
                add("ok", t, "anon UPDATE blocked (probe row unchanged)")
            else:
                add("CRITICAL", t, "anon can UPDATE rows (probe row changed)")
        # INSERT by anon, verified by response
        st, txt = request("POST", f"{base}/rest/v1/{t}", args.key, json.dumps(row).encode(), rep)
        if st in (200, 201):
            try:
                probe_ids += [r.get(pk) for r in json.loads(txt)]
            except (json.JSONDecodeError, AttributeError):
                pass
            add("CRITICAL", t, "anon can INSERT rows")
        elif st in (401, 403) or "42501" in txt or "row-level security" in txt:
            add("ok", t, f"anon INSERT blocked ({st})")
        else:
            add("check", t, f"anon INSERT returned {st} (not an RLS denial - review): {txt[:120]}")
        # DELETE by anon, verified via service key
        request("DELETE", f"{base}/rest/v1/{t}?{flt}", args.key)
        st, after = request("GET", f"{base}/rest/v1/{t}?{flt}&select={pk}", svc)
        if st == 200 and json.loads(after):
            add("ok", t, "anon DELETE blocked (probe row still exists)")
        elif st == 200:
            add("CRITICAL", t, "anon can DELETE rows (probe row gone)")
        else:
            add("check", t, f"could not verify DELETE ({st})")
    finally:
        for i in probe_ids:
            if i is not None:
                request("DELETE", f"{base}/rest/v1/{t}?{pk}=eq.{i}", svc)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=os.getenv("SUPABASE_URL"))
    ap.add_argument("--key", default=os.getenv("SUPABASE_ANON_KEY"), help="anon/publishable key")
    ap.add_argument("--tables", help="comma-separated; default: discover via the REST schema")
    ap.add_argument("--write-probe", metavar="TABLE", help="anon write test on one table (own projects only)")
    ap.add_argument("--probe-row", default="{}", help="JSON for the probe row (fill NOT NULL columns)")
    ap.add_argument("--probe-update", help="JSON patch anon will try to apply to the probe row")
    ap.add_argument("--pk", default="id", help="primary key column of the probe table")
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
        write_probe(base, args, add)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({"url": base, "key_role": role, "findings": findings}, f, ensure_ascii=False, indent=2)
    return 1 if any(f["severity"] == "CRITICAL" for f in findings) else 0


if __name__ == "__main__":
    sys.exit(main())

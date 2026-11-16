#!/usr/bin/env python3
"""Scan a built bundle (dist/, build/) for secrets with gitleaks - without false "clean".

Plain `gitleaks dir dist/` has two traps:
  * an empty or missing folder exits 0, which looks like "no leaks";
  * the same exit code can mean "leaks found" or "gitleaks crashed".
This wrapper refuses empty folders, separates findings from errors, and decodes every
JWT it finds: a Supabase anon key in the bundle is normal, a service_role key is critical.

Exit codes: 0 clean, 2 leaks found, 1 error (empty folder, gitleaks missing, crash).
Usage: python sec-audit/scan_build.py dist/
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

CONFIG = Path(__file__).with_name(".gitleaks.toml")
JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.(eyJ[A-Za-z0-9_-]{8,})\.[A-Za-z0-9_-]{8,}")


def jwt_role(payload_b64: str) -> str | None:
    try:
        payload_b64 += "=" * (-len(payload_b64) % 4)
        return json.loads(base64.urlsafe_b64decode(payload_b64)).get("role")
    except Exception:
        return None


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 1
    target = Path(sys.argv[1])
    files = [f for f in target.rglob("*") if f.is_file()] if target.is_dir() else []
    if not files:
        print(f"ERROR: {target} is missing or empty - build first, an empty scan proves nothing")
        return 1
    if not shutil.which("gitleaks"):
        print("ERROR: gitleaks not found (https://github.com/gitleaks/gitleaks, >= 8.30)")
        return 1

    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "report.json"
        proc = subprocess.run(
            ["gitleaks", "dir", str(target), "-c", str(CONFIG), "--no-banner",
             "--exit-code", "2", "--report-format", "json", "--report-path", str(report)],
            capture_output=True, text=True)
        if proc.returncode not in (0, 2):
            print(f"ERROR: gitleaks failed ({proc.returncode}): {proc.stderr.strip()[:300]}")
            return 1
        findings = json.loads(report.read_text() or "[]") if report.exists() else []

    # JWTs: decide by role, not by regex (an allowlist regex would also hide service_role keys).
    # The report stays in a temp dir and secrets are never printed.
    jwt_notes, kept = [], []
    for x in findings:
        m = JWT_RE.search(x.get("Secret", "") or x.get("Match", ""))
        role = jwt_role(m.group(1)) if m else None
        if x.get("RuleID") == "jwt" and role in ("anon", "authenticated"):
            jwt_notes.append(f"{x.get('File')}: JWT role={role} (public by design)")
            continue
        if role == "service_role":
            x["RuleID"] = "supabase-service-role-jwt"
        kept.append(x)
    findings = kept
    flagged = {x.get("File") for x in findings if x.get("RuleID") == "supabase-service-role-jwt"}
    for f in files:
        try:
            text = f.read_text(errors="ignore")
        except OSError:
            continue
        for m in JWT_RE.finditer(text):
            role = jwt_role(m.group(1))
            if role == "service_role" and str(f) not in flagged:
                flagged.add(str(f))
                findings.append({"RuleID": "supabase-service-role-jwt", "File": str(f)})

    print(f"Scanned {len(files)} files in {target}")
    for n in sorted(set(jwt_notes)):
        print(f"  info  {n}")
    for x in findings:
        print(f"  LEAK  {x.get('RuleID')}  {x.get('File')}:{x.get('StartLine', '?')}")
    print("RESULT:", "leaks found" if findings else "clean")
    return 2 if findings else 0


if __name__ == "__main__":
    sys.exit(main())

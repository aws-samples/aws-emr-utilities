#!/usr/bin/env python3
"""Fail if a non-public identifier is present in the tree.

Run by `make scan` and by CI. One implementation rather than shell regexes in
two places, because the shell version was wrong twice:

  * it matched the strings it was searching for, inside itself, so it failed
    every build until the scanner file was excluded from its own scan;
  * `\\b[0-9]{12}\\b` matched digits inside decimal numbers, so a perfectly
    ordinary `"delta_pct": 89.218026112593` was reported as an AWS account ID.

Account IDs are therefore matched only when not adjacent to a digit or a
decimal point, and generated output is skipped.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

# Directories that never contain source worth scanning.
SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "out", "runs", "share",
             "dist", "build", "node_modules"}
SKIP_SUFFIX = {".pyc", ".png", ".jpg", ".gif", ".ico", ".zip", ".gz", ".parquet"}

# The AWS documentation example account IDs are the only ones allowed.
ALLOWED_ACCOUNTS = {"111122223333", "444455556666"}

# Assembled from fragments so this file does not trip its own scan.
HOSTS = ["quip" + "-amazon", "code." + "amazon", "w." + "amazon",
         "a2z" + ".com", "corp." + "amazon", "isen" + "gard"]
ALIAS_WORDS = ["pro" + "serv", "Isengard" + "-Personal"]

CHECKS = [
    ("internal hostname or tool", re.compile("|".join(re.escape(h) for h in HOSTS), re.I)),
    ("internal package or bindle", re.compile("|".join(re.escape(a) for a in ALIAS_WORDS), re.I)),
    # 12 digits that are a whole token. Excluding adjacent digits and decimal
    # points is not enough: it still flagged the leading digits of the job id
    # "000002394080c0e5". An account ID is never glued to letters, so the
    # boundary spans alphanumerics and underscore too.
    ("possible AWS account ID",
     re.compile(r"(?<![A-Za-z0-9_.])(\d{12})(?![A-Za-z0-9_.])")),
]


def ignored(root: Path, paths: list[Path]) -> set[Path]:
    """Paths git ignores. Anything gitignored cannot leak through the repository,
    and a local file such as a real-account config would otherwise fail the scan
    on a developer machine while passing in CI."""
    if not (root / ".git").exists() or not paths:
        return set()
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "check-ignore", "--stdin"],
            input="\n".join(str(p) for p in paths), text=True,
            capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return set()
    return {Path(line) for line in proc.stdout.splitlines() if line}


def files(root: Path):
    candidates: list[Path] = []
    skip: set[Path] = set()
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        if p.suffix in SKIP_SUFFIX:
            continue
        if p.resolve() == Path(__file__).resolve():
            continue
        candidates.append(p)
    skip = ignored(root, candidates)
    for p in candidates:
        if p not in skip:
            yield p


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    findings: list[str] = []

    for path in files(root):
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binary or unreadable: nothing to leak in text form
        rel = path.relative_to(root)
        for lineno, line in enumerate(text.splitlines(), 1):
            for label, pattern in CHECKS:
                for m in pattern.finditer(line):
                    if label == "possible AWS account ID" and m.group(1) in ALLOWED_ACCOUNTS:
                        continue
                    findings.append(f"{rel}:{lineno}: {label}: {m.group(0)[:60]}")

    if findings:
        print(f"{len(findings)} finding(s):", file=sys.stderr)
        for f in findings:
            print(f"  {f}", file=sys.stderr)
        print("\nSee CONTRIBUTING.md. Use 111122223333 for example account IDs.",
              file=sys.stderr)
        return 1

    print("scan: no non-public identifiers found")
    return 0


if __name__ == "__main__":
    sys.exit(main())

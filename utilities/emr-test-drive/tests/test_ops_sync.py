#!/usr/bin/env python3
"""The default operation list exists in two places and must agree.

etd/assets/etd_job.py runs on the cluster and imports pyspark at module scope, so
the orchestrator cannot import it: doing so raised ModuleNotFoundError and killed
the one variant whose dispatch path needed it. The list is duplicated instead,
which is only safe if drift is detected.
"""
from __future__ import annotations

import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]


def literal(path: pathlib.Path, name: str):
    tree = ast.parse(path.read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == name:
                    return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not found in {path}")


def main() -> int:
    asset = literal(ROOT / "etd/assets/etd_job.py", "DEFAULT_OPERATIONS")
    live = literal(ROOT / "etd/live.py", "DEFAULT_OPERATIONS")
    if asset == live:
        print(f"  ok   DEFAULT_OPERATIONS agree ({len(asset)} operations)")
        return 0
    print("  FAIL DEFAULT_OPERATIONS differ")
    print(f"       only in asset: {[o for o in asset if o not in live]}")
    print(f"       only in live : {[o for o in live if o not in asset]}")
    return 1


if __name__ == "__main__":
    sys.exit(main())

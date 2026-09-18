#!/usr/bin/env python3
"""Security regression tests. Run by `make test`; no AWS account needed.

Each case corresponds to a finding from the pre-publication audit. They exist so
a future refactor cannot quietly reopen one.
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from etd.spec import ConfigError, load_spec  # noqa: E402

BASE = {
    "run": {
        "name": "sec-test",
        "region": "us-east-1",
        "account": "444455556666",
        "bucket": "some-bucket",
        "execution_role_arn": "arn:aws:iam::444455556666:role/r",
    },
    "variants": [
        {"id": "base", "release_label": "emr-7.11.0", "baseline": True},
        {"id": "cand", "release_label": "emr-7.13.0"},
    ],
    "workloads": [{"id": "perf", "kind": "performance"}],
}

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASSED if ok else FAILED).append(f"{name}{': ' + detail if detail else ''}")


def load(cfg: dict):
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(cfg, fh)
        path = fh.name
    try:
        return load_spec(path), None
    except ConfigError as exc:
        return None, str(exc)
    finally:
        Path(path).unlink(missing_ok=True)


def deep(**over):
    import copy
    cfg = copy.deepcopy(BASE)
    for k, v in over.items():
        if k == "variant_id":
            cfg["variants"][1]["id"] = v
        elif k == "prefix":
            cfg["run"]["prefix"] = v
        elif k == "workload_id":
            cfg["workloads"] = [{"id": v, "kind": "performance"}]  # replaces the default
    return cfg


def main() -> int:
    # The control: a clean config must still load.
    spec, err = load(deep())
    check("baseline config loads", spec is not None, err or "")

    # Variant and workload ids become path and S3 key segments.
    for bad in ("../../etc/passwd", "..", "a/b", "x\\y", "../cand", "a b"):
        spec, err = load(deep(variant_id=bad))
        check(f"variant id rejected: {bad!r}", spec is None and err is not None)

    # "/" is a legitimate grouping separator in a workload id and is folded to
    # "-" before use as a filename, so it must be accepted; ".." must not be.
    for bad in ("../../x", "..", "/abs", "a\\b", "x/../y"):
        spec, err = load(deep(workload_id=bad))
        check(f"workload id rejected: {bad!r}", spec is None and err is not None)
    for good in ("func/core", "perf/sql", "perf/tpcds-sf100"):
        spec, err = load(deep(workload_id=good))
        check(f"workload id accepted: {good!r}", spec is not None, err or "")

    # teardown --delete-data scopes S3 deletion with run.prefix.
    for bad in ("", "*", "../..", "a/b"):
        spec, err = load(deep(prefix=bad))
        check(f"prefix rejected: {bad!r}", spec is None and err is not None)

    for good in ("etd", "my-etd", "etd.v2"):
        spec, err = load(deep(prefix=good))
        check(f"prefix accepted: {good!r}", spec is not None, err or "")

    # The report must not let untrusted text break out of a title attribute or
    # close the JSON island early. Check against the generated example report.
    report = Path(__file__).resolve().parents[1] / "examples/offline/out/report.html"
    if report.exists():
        html = report.read_text()
        island = re.search(r'<script id="vmap"[^>]*>(.*?)</script>', html, re.S)
        check("vmap island present and parses",
              bool(island) and isinstance(json.loads(island.group(1)), dict))
        check("no raw '<' inside the JSON island",
              bool(island) and "<" not in island.group(1))
        # Every title attribute must be closed before the next tag begins; a raw
        # unescaped quote in tooltip text would produce a stray '>' inside one.
        bad_titles = [t for t in re.findall(r'title="([^"]*)"', html) if "<" in t or ">" in t]
        check("no markup inside title attributes", not bad_titles,
              f"{len(bad_titles)} suspicious" if bad_titles else "")
    else:
        check("report present for HTML checks", False, "run `make example` first")

    # Lake Formation data filter enforcement. A granted filter that returns more
    # than it permits is a disclosure, and it is the one failure mode that looks
    # like success from inside the job, so the detection path is asserted here.
    rj = Path(__file__).resolve().parents[1] / "examples/offline/out/report.json"
    if rj.exists():
        doc = json.loads(rj.read_text())
        findings, crit = [], []
        for cmp_ in doc.get("comparisons", []):
            for f in cmp_.get("correctness_findings", []) or []:
                if f.get("category") == "FILTER_NOT_ENFORCED":
                    findings.append(f)
                    if f.get("severity") == "critical":
                        crit.append(f)
        check("filter non-enforcement is detected", bool(findings),
              f"{len(findings)} finding(s)")
        check("filter non-enforcement is critical", bool(crit) and len(crit) == len(findings))
        check("finding carries the numbers that make it actionable",
              all(any(k == "rows visible" for k, _ in f["evidence"]) for f in findings))
        # A comparison containing one must not be PROCEED.
        blocked = [c["verdict"] for c in doc["comparisons"]
                   if any(f.get("category") == "FILTER_NOT_ENFORCED"
                          for f in c.get("correctness_findings", []) or [])]
        check("a run with a disclosure does not say PROCEED",
              blocked and all(v != "PROCEED" for v in blocked), f"verdicts={blocked}")

    # The filter check must be attributed to a principal filters apply to, and
    # must not be duplicated per table format.
    if rj.exists():
        doc = json.loads(rj.read_text())
        man = json.loads((Path(__file__).resolve().parents[1]
                          / "examples/offline/fixtures/run_manifest.json").read_text())
        lf = man.get("lake_formation") or {}
        check("a reader principal is recorded", bool(lf.get("filter_reader_arn")))
        check("the reader is not an LF administrator", lf.get("filter_reader_is_lf_admin") is False)
        check("enforcement is marked assertable", lf.get("filter_enforcement_assertable") is True)
        # Not-assertable path: clearing the flag must downgrade, not hide.
        import copy
        from etd.compare import compare_correctness
        units = [{"name": "ROW_FILTER", "table_format": "n/a", "status": "FAILED",
                  "filter_kind": "row", "rows_visible": 100, "rows_permitted": 10,
                  "columns_visible": ["a"], "columns_permitted": None,
                  "filter_over_disclosure": True, "expected_state": "S"}]
        def payload(assertable):
            return {"units": copy.deepcopy(units),
                    "run": {"lake_formation": {"filter_enforcement_assertable": assertable}}}
        hard = compare_correctness(payload(True), payload(True))
        soft = compare_correctness(payload(False), payload(False))
        cats_hard = {f["category"] for f in hard}
        cats_soft = {f["category"] for f in soft}
        check("assertable -> critical FILTER_NOT_ENFORCED",
              "FILTER_NOT_ENFORCED" in cats_hard
              and all(f["severity"] == "critical" for f in hard))
        check("not assertable -> informational, not hidden",
              "FILTER_ENFORCEMENT_NOT_ASSERTABLE" in cats_soft
              and all(f["severity"] == "info" for f in soft) and len(soft) == len(hard))

    # Filter units must not be duplicated per table format
    import glob as _g2
    dupes = []
    for path in _g2.glob(str(Path(__file__).resolve().parents[1]
                             / "examples/offline/fixtures/units/*func*")):
        d = json.loads(Path(path).read_text())
        fmts = {u.get("table_format") for u in d["units"] if u.get("filter_kind")}
        if fmts - {"n/a"}:
            dupes.append((d["variant_id"], sorted(f for f in fmts if f)))
    check("filter checks are not labelled per table format", not dupes, str(dupes))

    # Filters are only asserted under FGAC: with plain Glue or full table access
    # every row is legitimately visible, so asserting otherwise would invent a
    # finding. Verified against the generated fixtures.
    import glob as _glob
    fixtures = _glob.glob(str(Path(__file__).resolve().parents[1]
                              / "examples/offline/fixtures/units/*func*"))
    modes = {}
    for path in fixtures:
        doc = json.loads(Path(path).read_text())
        vid = doc["variant_id"]
        modes[vid] = sum(1 for u in doc["units"] if u.get("filter_kind"))
    fgac = {k: v for k, v in modes.items() if "fgac" in k}
    other = {k: v for k, v in modes.items() if "fgac" not in k}
    check("FGAC variants carry filter units", all(v > 0 for v in fgac.values()), str(fgac))
    check("non-FGAC variants carry none", all(v == 0 for v in other.values()), str(other))

    for line in PASSED:
        print(f"  ok   {line}")
    for line in FAILED:
        print(f"  FAIL {line}")
    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())

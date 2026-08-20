"""EMR Test Drive — comparison engine (preview implementation).

Four independent diffs, in reporting priority order:

  1. correctness  — did we get the right data?      (highest priority)
  2. functional   — did the operation work as expected against the documented matrix?
  3. performance  — best-of-N per unit, noise band, geomean of ratios
  4. cost         — normalised $ per run

Plus variant matching (the UNMATCHED gate) and error clustering.

Standard library only. No AWS calls.
"""

from __future__ import annotations

import json
import math
import re
import statistics
from dataclasses import dataclass, field
from pathlib import Path

# Dimensions that define a variant's identity for matching purposes.
# config_hash is deliberately excluded: it changes as a *consequence* of a
# release-label change, so counting it would produce false UNMATCHED verdicts.
PRIMARY_DIMS = ["deployment_model", "release_label", "architecture",
                "access_mode", "shape_hash", "patch_hash"]

STATE_LABEL = {
    "S": "Supported",
    "S3": "Supported with S3 IAM",
    "N": "Not supported",
    "NA": "Not applicable",
}

SEV_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}


# --------------------------------------------------------------------- loading


@dataclass
class Run:
    manifest: dict
    units: dict = field(default_factory=dict)   # (variant_id, workload_id) -> payload

    @property
    def variants(self) -> dict:
        return {v["variant_id"]: v for v in self.manifest["variants"]}

    @property
    def workloads(self) -> dict:
        return {w["workload_id"]: w for w in self.manifest["workloads"]}

    def payload(self, variant_id: str, workload_id: str) -> dict | None:
        doc = self.units.get((variant_id, workload_id))
        if doc is None:
            return None
        # Run-level facts some comparisons need -- for instance whether the job
        # role is a Lake Formation administrator, which decides whether filter
        # enforcement can be asserted at all. Shallow copy: the cached document
        # is not mutated.
        return {**doc, "run": self.manifest}


def slug(workload_id: str) -> str:
    return workload_id.replace("/", "-")


def load_run(fixtures_dir: Path) -> Run:
    manifest = json.loads((fixtures_dir / "run_manifest.json").read_text())
    run = Run(manifest=manifest)
    for v in manifest["variants"]:
        for w in manifest["workloads"]:
            p = fixtures_dir / "units" / f"{v['variant_id']}__{slug(w['workload_id'])}.json"
            if p.exists():
                run.units[(v["variant_id"], w["workload_id"])] = json.loads(p.read_text())
    return run


# ------------------------------------------------------------ variant matching


def variant_diff(a: dict, b: dict) -> list[str]:
    return [d for d in PRIMARY_DIMS if a.get(d) != b.get(d)]


def match_status(a: dict, b: dict, comparison: dict) -> dict:
    dims = variant_diff(a, b)
    advisory = []
    if a.get("config_hash") != b.get("config_hash"):
        advisory.append("config_hash differs (expected when the release label changes)")

    if len(dims) == 1:
        status, why = "MATCHED", f"differs only by {dims[0]}"
    elif comparison.get("sizing_caveat"):
        status = "UNMATCHED_BY_DESIGN"
        why = f"differs by {', '.join(dims)} — declared: {comparison['sizing_caveat']}"
    else:
        status = "UNMATCHED"
        why = (f"differs by {len(dims)} dimensions ({', '.join(dims)}) — "
               "performance verdict suppressed; functional verdict still valid")
    return {"status": status, "dims": dims, "why": why, "advisory": advisory,
            "perf_verdict_valid": status == "MATCHED"}


# ------------------------------------------------------------- error clustering

_NORMALISERS = [
    (re.compile(r"\b[0-9a-f]{8,}\b", re.I), "<hex>"),
    (re.compile(r"\bRequest ID: \S+"), "Request ID: <id>"),
    (re.compile(r"s3://[^\s,;)]+"), "s3://<path>"),
    (re.compile(r"arn:aws:[^\s,;)]+"), "<arn>"),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}\S*"), "<ts>"),
    (re.compile(r"\b\d+\b"), "<n>"),
    (re.compile(r"\s+"), " "),
]


def normalise_error(msg: str) -> str:
    out = msg
    for pat, repl in _NORMALISERS:
        out = pat.sub(repl, out)
    return out.strip()[:400]


def cluster_errors(rows: list[dict]) -> list[dict]:
    """Group findings that carry an error message into root-cause clusters."""
    buckets: dict[str, dict] = {}
    for r in rows:
        msg = r.get("error")
        if not msg:
            continue
        key = normalise_error(msg)
        b = buckets.setdefault(key, {
            "cluster_id": f"ec-{abs(hash(key)) % 100000:05d}",
            "signature": key,
            "representative": msg,
            "count": 0,
            "members": [],
        })
        b["count"] += 1
        b["members"].append(r.get("unit_label", r.get("name", "?")))
    return sorted(buckets.values(), key=lambda b: -b["count"])


# ------------------------------------------------------------- functional diff


def _fkey(u: dict) -> tuple:
    return (u["name"], u["table_format"])


def compare_functional(base: dict, cand: dict) -> dict:
    b = {_fkey(u): u for u in base["units"]}
    c = {_fkey(u): u for u in cand["units"]}
    rows = []

    for key in sorted(set(b) | set(c)):
        bu, cu = b.get(key), c.get(key)
        op, fmt = key
        label = f"{fmt}.{op}"
        if not bu or not cu:
            rows.append({"unit_label": label, "operation": op, "table_format": fmt,
                         "verdict": "MISSING", "severity": "info",
                         "base_status": bu["status"] if bu else "-",
                         "cand_status": cu["status"] if cu else "-",
                         "expected_base": (bu or {}).get("expected_state", "-"),
                         "expected_cand": (cu or {}).get("expected_state", "-"),
                         "error": None, "table_type": (cu or bu).get("table_type"),
                         # Same keys as a compared row. An operation present on
                         # one side only is a legitimate asymmetry -- the Lake
                         # Formation filter operations exist only under FGAC --
                         # and the report should show it, not fail to render.
                         "base_duration_s": (bu or {}).get("duration_s"),
                         "cand_duration_s": (cu or {}).get("duration_s"),
                         "lf_permissions": (cu or bu).get("lf_permissions") or []})
            continue

        bs, cs = bu["status"], cu["status"]
        be, ce = bu["expected_state"], cu["expected_state"]

        if "NOT_APPLICABLE" in (bs, cs):
            verdict, sev = "NOT_COMPARABLE", "info"
        elif cs == "FLAKY" or bs == "FLAKY":
            verdict, sev = "FLAKY", "medium"
        elif be == "N" and ce in ("S", "S3") and bs == "FAILED" and cs == "SUCCESS":
            verdict, sev = "FIXED_BY_RELEASE", "info"
        elif be in ("S", "S3") and ce == "N":
            verdict, sev = "EXPECTED_REMOVED", "medium"
        elif be == "N" and ce == "N":
            verdict, sev = "EXPECTED_UNSUPPORTED", "info"
        elif bs == "SUCCESS" and cs == "FAILED":
            verdict, sev = "NEW_FAILURE", "critical"
        elif bs == "FAILED" and cs == "SUCCESS":
            verdict, sev = "FIXED", "info"
        elif bs == "FAILED" and cs == "FAILED":
            verdict, sev = "STABLE_FAIL", "high"
        else:
            verdict, sev = "STABLE_PASS", "info"

        rows.append({
            "unit_label": label, "operation": op, "table_format": fmt,
            "table_type": cu.get("table_type"),
            "verdict": verdict, "severity": sev,
            "base_status": bs, "cand_status": cs,
            "expected_base": be, "expected_cand": ce,
            "expected_reason": cu.get("expected_reason"),
            "lf_permissions": cu.get("lf_permissions", []),
            "error": cu.get("error") if verdict in ("NEW_FAILURE", "STABLE_FAIL", "FLAKY") else None,
            "defect_note": cu.get("defect_note"),
            "base_duration_s": bu.get("duration_s"),
            "cand_duration_s": cu.get("duration_s"),
            "job_id": cu.get("job_id"),
        })

    counts: dict[str, int] = {}
    for r in rows:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    return {"rows": rows, "counts": counts,
            "clusters": cluster_errors([r for r in rows if r["error"]])}


# ------------------------------------------------------------ correctness diff


def _defect_flags(u: dict | None) -> set[str]:
    """Which correctness defects does this unit exhibit, judged on its own?"""
    if not u or u.get("status") != "SUCCESS":
        return set()
    flags = set()
    if u.get("table_version_advanced") is False:
        flags.add("SILENT_DATA_LOSS")
    if u.get("orphaned_object_count"):
        flags.add("ORPHANED_DATA")
    # A granted Lake Formation data filter that returned more than it permits.
    if u.get("filter_over_disclosure"):
        flags.add("FILTER_NOT_ENFORCED")
    return flags


def compare_correctness(base: dict, cand: dict) -> list[dict]:
    b = {_fkey(u): u for u in base["units"]}
    findings = []

    # Formats where at least one operation changed outcome between the variants.
    # When an operation goes from failing to passing (or the reverse), every later
    # operation on that table sees legitimately different state, so a content
    # checksum difference is a cascade rather than a correctness defect. Reporting
    # it would flag the upgrade's own improvements as data divergence.
    changed_formats = set()
    for cu in cand["units"]:
        bu = b.get(_fkey(cu))
        if bu and bu.get("status") != cu.get("status"):
            changed_formats.add(cu["table_format"])

    # Defects present on the baseline that the candidate no longer exhibits.
    # Without this, a patch that *repairs* a defect gets reported as a
    # divergence against the (broken) baseline — exactly backwards.
    c_by_key = {_fkey(u): u for u in cand["units"]}
    for key, bu in b.items():
        cu = c_by_key.get(key)
        gone = _defect_flags(bu) - _defect_flags(cu)
        for cat in sorted(gone):
            if not cu or cu.get("status") != "SUCCESS":
                continue
            findings.append({
                "category": "CORRECTNESS_FIXED", "severity": "info",
                "unit_label": f"{bu['table_format']}.{bu['name']}",
                "table_format": bu["table_format"], "table_type": cu.get("table_type"),
                "summary": f"{cat.replace('_', ' ').title()} present on the baseline is no longer reproducible on the candidate",
                "evidence": [
                    ("defect on baseline", cat),
                    ("baseline rows readable", str(bu.get("row_count"))),
                    ("candidate rows readable", str(cu.get("row_count"))),
                    ("baseline version advanced", str(bu.get("table_version_advanced"))),
                    ("candidate version advanced", str(cu.get("table_version_advanced"))),
                ],
                "note": None, "job_id": cu.get("job_id"), "pre_existing": False,
                "resolved": True,
            })

    for cu in cand["units"]:
        key = _fkey(cu)
        bu = b.get(key)
        label = f"{cu['table_format']}.{cu['name']}"
        if cu["status"] != "SUCCESS":
            continue

        # 1. exit 0 but the table version did not move
        if cu.get("table_version_advanced") is False:
            findings.append({
                "category": "SILENT_DATA_LOSS", "severity": "critical", "unit_label": label,
                "table_format": cu["table_format"], "table_type": cu.get("table_type"),
                "summary": "Operation reported SUCCESS but the table commit log did not advance",
                "evidence": [
                    ("exit status", "SUCCESS (0)"),
                    ("rows expected", "5000"),
                    ("rows readable after commit", str(cu.get("row_count"))),
                    ("table version advanced", "no"),
                    ("baseline behaviour", "version advanced, 5000 rows readable" if bu and bu.get("table_version_advanced") else "n/a"),
                ],
                "note": cu.get("defect_note"),
                "job_id": cu.get("job_id"),
                "pre_existing": bool(bu and bu.get("table_version_advanced") is False),
            })

        # 2. overwrite left prior-generation files behind
        if cu.get("orphaned_object_count"):
            findings.append({
                "category": "ORPHANED_DATA", "severity": "high", "unit_label": label,
                "table_format": cu["table_format"], "table_type": cu.get("table_type"),
                "summary": "Overwrite left prior-generation objects at the table prefix",
                "evidence": [
                    ("orphaned objects", str(cu["orphaned_object_count"])),
                    ("orphaned bytes", f"{cu.get('orphaned_bytes', 0):,}"),
                    ("risk", "reads may return duplicate or stale rows"),
                ],
                "note": cu.get("defect_note"),
                "job_id": cu.get("job_id"),
                "pre_existing": bool(bu and bu.get("orphaned_object_count")),
            })

        # 3. same operation, different result content
        if bu and bu.get("result_checksum") and cu.get("result_checksum") \
                and bu["status"] == "SUCCESS" \
                and bu["result_checksum"] != cu["result_checksum"] \
                and not (_defect_flags(bu) - _defect_flags(cu)) \
                and cu["table_format"] not in changed_formats:
            findings.append({
                "category": "DIVERGENT_RESULT", "severity": "critical", "unit_label": label,
                "table_format": cu["table_format"], "table_type": cu.get("table_type"),
                "summary": "Result set checksum differs between baseline and candidate",
                "evidence": [
                    ("baseline rows", str(bu.get("row_count"))),
                    ("candidate rows", str(cu.get("row_count"))),
                    ("baseline checksum", str(bu.get("result_checksum"))),
                    ("candidate checksum", str(cu.get("result_checksum"))),
                ],
                "note": cu.get("defect_note"),
                "job_id": cu.get("job_id"),
                "pre_existing": False,
            })

    # de-duplicate: a silent-data-loss unit also trips the checksum test
    seen = set()
    out = []
    # Lake Formation data filter not enforced. Reported on the candidate whether
    # or not the baseline did the same: unlike a performance delta, "both sides
    # disclose too much" is not a reason to stay quiet.
    # An administrator bypasses data cell filters by design, so a full-table read
    # is expected and reporting it as a disclosure is a false alarm. The finding is
    # kept -- the run still could not verify enforcement -- but as information
    # rather than a critical defect, and it no longer blocks the verdict.
    # Assertable only when the filter check ran as a principal that filters
    # actually apply to. Before the reader role existed this ran as the job role,
    # which is typically a Lake Formation administrator, so a full-table read was
    # correct behaviour being reported as a disclosure.
    lf = ((cand.get("run") or {}).get("lake_formation")
          or (base.get("run") or {}).get("lake_formation") or {})
    assertable = bool(lf.get("filter_enforcement_assertable"))
    for cu in cand["units"]:
        if not cu.get("filter_over_disclosure"):
            continue
        label = f"{cu['table_format']}.{cu['name']}"
        bu = b.get(_fkey(cu))
        findings.append({
            "category": ("FILTER_NOT_ENFORCED" if assertable
                         else "FILTER_ENFORCEMENT_NOT_ASSERTABLE"),
            "severity": "critical" if assertable else "info",
            "not_assertable_reason": (None if assertable else
                ("no non-administrator reader principal was available, so the filter "
                 "check ran as a principal that bypasses data cell filters; "
                 "enforcement was not tested")),
            "unit_label": label,
            "table_format": cu["table_format"], "table_type": cu.get("table_type"),
            "pre_existing": bool(bu and bu.get("filter_over_disclosure")),
            "summary": (f"Lake Formation {cu.get('filter_kind', 'data')} filter returned more "
                        f"than it permits: rows visible "
                        f"{cu.get('rows_visible')} of {cu.get('rows_permitted')} permitted"),
            "evidence": [
                ("filter kind", cu.get("filter_kind")),
                ("rows visible", cu.get("rows_visible")),
                ("rows permitted", cu.get("rows_permitted")),
                ("columns visible", ", ".join(cu.get("columns_visible") or [])),
                ("columns permitted", ", ".join(cu.get("columns_permitted") or []) or "all"),
                ("row filter enforced", cu.get("row_filter_enforced")),
                ("column filter enforced", cu.get("column_filter_enforced")),
            ],
        })

    for f in sorted(findings, key=lambda f: (SEV_ORDER[f["severity"]], f["unit_label"])):
        k = (f["unit_label"], f["category"])
        if k in seen:
            continue
        if f["category"] == "DIVERGENT_RESULT" and (f["unit_label"], "SILENT_DATA_LOSS") in {
                (x["unit_label"], x["category"]) for x in findings}:
            continue
        seen.add(k)
        out.append(f)
    return out


# ------------------------------------------------------------- performance diff


def _best(u: dict) -> float | None:
    return min(u["iterations"]) if u.get("iterations") else None


def _spread_pct(u: dict) -> float | None:
    """Observed variance for this unit, as a percentage of its best time.

    Two sources, kept separate on purpose:

      within-job  — variance between iterations inside one job run, computed per
                    run and then maxed. Measures query-level jitter.
      between-job — variance of the best time across independent job runs.
                    Measures environment: host, placement, warm vs cold capacity.

    Pooling all iterations from all runs into one list would conflate the two and
    inflate the band, which hides real, reproducible deltas. So take the max of
    the two measures rather than the spread of the pooled set.
    """
    per_job = u.get("per_job_iterations")
    withins: list[float] = []
    if per_job:
        for run in per_job:
            if run and len(run) >= 2 and min(run):
                withins.append((max(run) - min(run)) / min(run) * 100.0)
    else:
        it = u.get("iterations") or []
        if len(it) >= 2 and min(it):
            withins.append((max(it) - min(it)) / min(it) * 100.0)
    between = u.get("between_job_spread_pct")
    vals = [x for x in (max(withins) if withins else None, between) if x is not None]
    return max(vals) if vals else None


def compare_perf(base: dict, cand: dict, thresholds: dict, intent: str = "upgrade_regression") -> dict:
    """Diff per-unit performance.

    ``intent`` matters. For an upgrade or patch comparison a slowdown is a
    REGRESSION. For a governance comparison (plain -> FTA -> FGAC) the same
    slowdown is the *expected price of enabling governance*, so it is labelled
    OVERHEAD and excluded from the pass/fail verdict. Reporting it as a
    regression would tell the customer to stop enforcing access control.
    """
    noise = thresholds["perf_noise_band_pct"]
    alert = thresholds["perf_regression_alert_pct"]
    min_it = thresholds["min_iterations_for_perf_verdict"]
    governance = intent == "governance_overhead"

    b = {u["name"]: u for u in base["units"]}
    rows, ratios = [], []

    for cu in cand["units"]:
        bu = b.get(cu["name"])
        if not bu:
            continue
        bb, cb = _best(bu), _best(cu)

        if cu["status"] == "TIMEOUT" and bu["status"] != "TIMEOUT":
            verdict, sev, delta = "NEW_TIMEOUT", "critical", None
        elif bu["status"] == "TIMEOUT" and cu["status"] != "TIMEOUT":
            verdict, sev, delta = "RESOLVED_TIMEOUT", "info", None
        elif bb is None or cb is None:
            verdict, sev, delta = "NO_DATA", "info", None
        elif len(bu["iterations"]) < min_it or len(cu["iterations"]) < min_it:
            verdict, sev, delta = "INSUFFICIENT_DATA", "info", (cb - bb) / bb * 100.0
        else:
            delta = (cb - bb) / bb * 100.0
            ratios.append(cb / bb)
            # The band is per query and never tighter than the noise we actually
            # observed on either side. A query whose own iterations vary by 40%
            # cannot evidence a 20% regression, and saying otherwise is how
            # benchmark reports lose credibility.
            band = max(noise, _spread_pct(bu) or 0.0, _spread_pct(cu) or 0.0)
            if abs(delta) <= band:
                verdict, sev = "NEUTRAL", "info"
                if band > noise:
                    verdict = "WITHIN_NOISE"
            elif delta > 0:
                if governance:
                    verdict, sev = "OVERHEAD", "info"
                else:
                    verdict = "REGRESSION"
                    sev = "high" if delta >= alert else "medium"
            else:
                verdict, sev = "IMPROVEMENT", "info"

        rows.append({
            "name": cu["name"], "verdict": verdict, "severity": sev,
            "base_best_s": bb, "cand_best_s": cb,
            # rounded like the aggregate percentages; the raw float carried
            # twelve decimal places of noise into the artifact
            "delta_pct": round(delta, 1) if delta is not None else None,
            "base_iterations": bu.get("iterations", []),
            "cand_iterations": cu.get("iterations", []),
            "base_spread_pct": _spread_pct(bu), "cand_spread_pct": _spread_pct(cu),
            "effective_band_pct": round(max(noise, _spread_pct(bu) or 0.0,
                                            _spread_pct(cu) or 0.0), 1),
            "job_id": cu.get("job_id"), "error": cu.get("error"),
        })

    ok = [r for r in rows if r["base_best_s"] and r["cand_best_s"]]
    total_b = sum(r["base_best_s"] for r in ok)
    total_c = sum(r["cand_best_s"] for r in ok)
    geo = math.exp(statistics.fmean(math.log(x) for x in ratios)) if ratios else None
    sorted_ratios = sorted(ratios)

    def pct(p: float):
        if not sorted_ratios:
            return None
        i = min(len(sorted_ratios) - 1, int(round(p / 100 * (len(sorted_ratios) - 1))))
        return sorted_ratios[i]

    counts: dict[str, int] = {}
    for r in rows:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1

    return {
        "rows": sorted(rows, key=lambda r: -(r["delta_pct"] if r["delta_pct"] is not None else -999)),
        "counts": counts,
        "aggregate": {
            "queries_compared": len(ok),
            "total_base_s": round(total_b, 1),
            "total_cand_s": round(total_c, 1),
            "total_delta_pct": round((total_c - total_b) / total_b * 100.0, 1) if total_b else None,
            "geomean_ratio": round(geo, 4) if geo else None,
            "geomean_delta_pct": round((geo - 1) * 100, 1) if geo else None,
            "p50_ratio": round(pct(50), 4) if ratios else None,
            "p95_ratio": round(pct(95), 4) if ratios else None,
            "max_base_spread_pct": round(max((r["base_spread_pct"] or 0) for r in rows), 1) if rows else None,
            "max_cand_spread_pct": round(max((r["cand_spread_pct"] or 0) for r in rows), 1) if rows else None,
            "noise_band_pct": noise,
        },
    }


# --------------------------------------------------------------------- cost


def usd(cost_facts: dict, pricing: dict) -> float:
    p = pricing["emr_serverless_x86_64"]
    return (cost_facts["vcpu_hour"] * p["vcpu_hour_usd"]
            + cost_facts["memory_gb_hour"] * p["memory_gb_hour_usd"]
            + cost_facts.get("storage_gb_hour", 0) * p.get("storage_gb_hour_usd", 0))


def compare_cost(run: Run, base_id: str, cand_id: str) -> list[dict]:
    out = []
    for wid in run.workloads:
        bp, cp = run.payload(base_id, wid), run.payload(cand_id, wid)
        if not bp or not cp:
            continue
        bc, cc = usd(bp["cost_facts"], run.manifest["pricing"]), usd(cp["cost_facts"], run.manifest["pricing"])
        out.append({
            "workload_id": wid,
            "base_usd": round(bc, 4), "cand_usd": round(cc, 4),
            "delta_pct": round((cc - bc) / bc * 100.0, 1) if bc else None,
            "base_wall_s": bp["cost_facts"]["wall_clock_s"],
            "cand_wall_s": cp["cost_facts"]["wall_clock_s"],
            "base_vcpu_hour": bp["cost_facts"]["vcpu_hour"],
            "cand_vcpu_hour": cp["cost_facts"]["vcpu_hour"],
            "base_gb_hour": bp["cost_facts"]["memory_gb_hour"],
            "cand_gb_hour": cp["cost_facts"]["memory_gb_hour"],
            "base_drivers": bp["cost_facts"].get("drivers_per_job"),
            "cand_drivers": cp["cost_facts"].get("drivers_per_job"),
        })
    return out


# ------------------------------------------------------------------- verdicts


def overall_verdict(fn: dict, corr: list[dict], perf: dict | None, match: dict,
                    intent: str = "upgrade_regression") -> dict:
    reasons = []
    governance = intent == "governance_overhead"
    level = "PROCEED"

    crit = [f for f in corr if f["severity"] == "critical" and not f["pre_existing"]]
    if crit:
        level = "BLOCK"
        reasons.append(f"{len(crit)} new correctness finding(s) — data may be wrong while the job reports success")

    nf = fn["counts"].get("NEW_FAILURE", 0) if fn else 0
    if nf:
        level = "BLOCK"
        label = ("operation(s) that work today fail once this is enabled" if governance
                 else "operation(s) passed on the baseline and fail on the candidate")
        reasons.append(f"{nf} {label}")

    # Performance only gates upgrade/patch comparisons, and only when matched.
    if perf and match["perf_verdict_valid"] and not governance:
        nt = perf["counts"].get("NEW_TIMEOUT", 0)
        if nt:
            level = "BLOCK"
            reasons.append(f"{nt} query/queries newly time out")
        high = [r for r in perf["rows"] if r["verdict"] == "REGRESSION" and r["severity"] == "high"]
        if high and level != "BLOCK":
            level = "CAUTION"
        if high:
            reasons.append(f"{len(high)} query/queries regressed beyond the alert threshold")
    elif perf and governance:
        a = perf["aggregate"]
        if a["geomean_delta_pct"] is not None:
            reasons.append(f"governance overhead measured at {a['geomean_delta_pct']:+.1f}% "
                           f"geomean per query (not counted as a regression)")
        nt = perf["counts"].get("NEW_TIMEOUT", 0)
        if nt:
            level = "BLOCK" if level != "BLOCK" else level
            reasons.append(f"{nt} query/queries do not complete once this is enabled")

    sf = fn["counts"].get("STABLE_FAIL", 0) if fn else 0
    if sf and level == "PROCEED":
        level = "CAUTION"
    if sf:
        reasons.append(f"{sf} operation(s) fail on both sides despite being documented as supported (pre-existing)")

    pre = [f for f in corr if f["pre_existing"]]
    if pre:
        reasons.append(f"{len(pre)} pre-existing correctness finding(s) carried by both variants")

    res = [f for f in corr if f.get("resolved")]
    if res:
        reasons.append(f"{len(res)} correctness finding(s) present on the baseline are resolved on the candidate")

    fx = (fn["counts"].get("FIXED", 0) + fn["counts"].get("FIXED_BY_RELEASE", 0)) if fn else 0
    if fx:
        reasons.append(f"{fx} operation(s) improved: previously failing, now passing")

    # An unmatched pair cannot yield a clean verdict, only observations.
    if match["status"] == "UNMATCHED" and level == "PROCEED":
        level = "INDETERMINATE"
        reasons.insert(0, "Variants differ in more than one dimension — no upgrade verdict can be "
                          "drawn from this pair; use it for observation only")

    if not reasons:
        reasons.append("No correctness, functional or performance regressions detected")
    return {"level": level, "reasons": reasons, "intent": intent}


def build_comparison(run: Run, comparison: dict) -> dict:
    variants = run.variants
    b_id, c_id = comparison["baseline"], comparison["candidate"]
    bv, cv = variants[b_id], variants[c_id]
    match = match_status(bv, cv, comparison)

    func_wid = next((w for w in run.workloads if run.workloads[w]["kind"] == "functional"), None)
    # Every performance workload, in declaration order. A run can carry one per
    # data scale (100g, 1t, 3t) and comparing only the first would silently
    # discard the rest -- worse than not measuring them, because the report would
    # look complete.
    perf_wids = [w for w in run.workloads if run.workloads[w]["kind"] == "performance"]
    perf_wid = perf_wids[0] if perf_wids else None

    fn = None
    if func_wid and run.payload(b_id, func_wid) and run.payload(c_id, func_wid):
        fn = compare_functional(run.payload(b_id, func_wid), run.payload(c_id, func_wid))

    corr = []
    if func_wid and run.payload(b_id, func_wid) and run.payload(c_id, func_wid):
        corr = compare_correctness(run.payload(b_id, func_wid), run.payload(c_id, func_wid))

    performances = []
    for wid in perf_wids:
        bp, cp = run.payload(b_id, wid), run.payload(c_id, wid)
        if not bp or not cp:
            continue
        performances.append({
            "workload_id": wid,
            "label": run.workloads[wid].get("label") or wid,
            "perf": compare_perf(bp, cp, run.manifest["thresholds"],
                                 intent=comparison.get("intent", "upgrade_regression")),
        })
    perf = performances[0]["perf"] if performances else None

    # The verdict must consider every scale: a regression that only appears at the
    # largest one is the whole point of measuring more than one. Counts are summed
    # so the existing thresholds apply unchanged; the per-scale aggregates are left
    # untouched for display, because a geomean across scales would be meaningless.
    perf_for_verdict = perf
    if len(performances) > 1:
        merged_counts: dict = {}
        rows: list = []
        for entry in performances:
            for k, v in (entry["perf"].get("counts") or {}).items():
                if isinstance(v, (int, float)):
                    merged_counts[k] = merged_counts.get(k, 0) + v
            rows.extend(entry["perf"].get("rows") or [])
        perf_for_verdict = {**perf, "counts": merged_counts, "rows": rows}

    cost = compare_cost(run, b_id, c_id)
    verdict = overall_verdict(fn or {"counts": {}}, corr, perf_for_verdict, match,
                              intent=comparison.get("intent", "upgrade_regression"))

    return {
        "comparison": comparison, "baseline": bv, "candidate": cv, "match": match,
        "functional": fn, "correctness": corr, "performance": perf,
        "performances": performances, "cost": cost,
        "verdict": verdict,
    }


def derive_intent(a: dict, b: dict) -> str:
    """Classify a pair the user picked ad hoc.

    Access mode dominates: turning governance on is never a regression, it is a
    cost. Then a patch, then a release change; anything else is a config A/B.
    """
    if a.get("access_mode") != b.get("access_mode"):
        return "governance_overhead"
    if a.get("patch_hash") != b.get("patch_hash"):
        return "patch_validation"
    if a.get("release_label") != b.get("release_label"):
        return "upgrade_regression"
    return "upgrade_regression"


def pair_id(src: str, dst: str) -> str:
    return f"pair--{src}--{dst}"


def build_matrix(run: Run, max_pairs: int = 40) -> list[dict]:
    """Every ordered variant pair, with the declared comparisons' metadata kept.

    Rendering all pairs is what lets the report offer a source and destination
    dropdown instead of a fixed set of tabs: whatever the reader picks, the
    answer is already there.
    """
    declared = {(c["baseline"], c["candidate"]): c for c in run.manifest.get("comparisons", [])}
    ids = [v["variant_id"] for v in run.manifest["variants"]]
    labels = {v["variant_id"]: v.get("label", v["variant_id"]) for v in run.manifest["variants"]}

    out: list[dict] = []
    for src in ids:
        for dst in ids:
            if src == dst:
                continue
            d = declared.get((src, dst))
            a, b = run.variants[src], run.variants[dst]
            comparison = {
                "comparison_id": pair_id(src, dst),
                "declared_id": (d or {}).get("comparison_id"),
                "title": (d or {}).get("title") or f"{labels[src]} → {labels[dst]}",
                "baseline": src, "candidate": dst,
                "intent": (d or {}).get("intent") or derive_intent(a, b),
                "primary": bool((d or {}).get("primary")),
                "declared": d is not None,
            }
            if (d or {}).get("sizing_caveat"):
                comparison["sizing_caveat"] = d["sizing_caveat"]
            elif a.get("shape_hash") != b.get("shape_hash") and \
                    a.get("access_mode") != b.get("access_mode"):
                comparison["sizing_caveat"] = (
                    "Shapes differ because the access modes differ; fine-grained access control "
                    "runs a second driver and cannot be executor-matched. Read as overhead, not "
                    "as a matched benchmark.")
            out.append(comparison)
            if len(out) >= max_pairs:
                return out
    return out


def build_all(run: Run) -> list[dict]:
    return [build_comparison(run, c) for c in run.manifest["comparisons"]]


def build_all_pairs(run: Run) -> list[dict]:
    return [build_comparison(run, c) for c in build_matrix(run)]

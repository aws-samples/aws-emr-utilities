#!/usr/bin/env python3
"""Generate SYNTHETIC result fixtures for the EMR Test Drive example report.

The *expected support matrix* these results are diffed against is real data
transcribed from AWS documentation (etd/matrices/lf_fgac.json).
The *observed results* generated here are synthetic and deterministic — they
exist so the comparison engine and the report have something realistic to
chew on before any AWS resources are launched.

The injected defects deliberately mirror behaviour shapes that are either
documented by AWS (the credential switch at the 7.12 boundary, DELETE/UPDATE/MERGE
gating) or are generic failure shapes worth being able to detect at all:
parent-prefix listing denial on a staging location, a commit log that does not
advance, and an overwrite that leaves orphaned files behind.

Usage:  python3 make_fixtures.py
Output: fixtures/units/<variant_id>__<workload>.json
"""

from __future__ import annotations

import hashlib
import json
import pathlib
from pathlib import Path

HERE = Path(__file__).parent
FIX = HERE / "fixtures"
UNITS = FIX / "units"

TABLE_TYPES = ["managed", "managed_partitioned", "external", "external_partitioned"]

# ---------------------------------------------------------------- determinism


def jitter(seed: str, lo: float, hi: float) -> float:
    """Stable pseudo-random float in [lo, hi) derived from a seed string."""
    h = hashlib.sha256(seed.encode()).digest()
    frac = int.from_bytes(h[:8], "big") / float(1 << 64)
    return lo + frac * (hi - lo)


def pick(seed: str, options: list):
    h = hashlib.sha256(seed.encode()).digest()
    return options[int.from_bytes(h[8:12], "big") % len(options)]


def checksum(seed: str) -> str:
    return "sha256:" + hashlib.sha256(seed.encode()).hexdigest()[:16]


def release_num(release_label: str) -> tuple[int, ...]:
    return tuple(int(p) for p in release_label.replace("emr-", "").split("."))


def ge(release_label: str, gate: str) -> bool:
    return release_num(release_label) >= tuple(int(p) for p in gate.split("."))


# ------------------------------------------------------------ expected states


def expected_state(fmt: str, op: str, entry: dict, variant: dict) -> tuple[str, str]:
    """Resolve the expected support state for this op on this variant.

    Returns (state_code, why). State codes: S, S3, N, NA.
    """
    mode = variant["access_mode"]
    rel = variant["release_label"]
    is_filter = op.endswith("_FILTER")

    if mode == "plain":
        if is_filter:
            return "NA", "no Lake Formation filtering in plain mode"
        return "S", "plain Glue catalog with runtime-role IAM"

    if mode == "lf_fta":
        if is_filter:
            return "NA", "FTA grants whole-table access; row/column/cell filters do not apply"
        # FTA vends whole-table credentials, so the FGAC per-op matrix does not bind.
        return "S", "FTA whole-table credential vending"

    # lf_fgac — the documented matrix binds, subject to release gates.
    gate = entry.get("min_release")
    if gate and not ge(rel, gate):
        return "N", f"not supported under FGAC before EMR {gate} (documented)"
    if op in ("DELETE", "UPDATE", "MERGE_INTO") and not ge(rel, "7.12.0"):
        return "N", "EMR 7.11 and older do not support DELETE/UPDATE/MERGE under FGAC (documented)"
    return entry["state"], entry.get("caveat", "documented support state")


# ------------------------------------------------------------ injected defects

# (variant_id, format, operation) -> defect descriptor
DEFECTS = {
    ("v-713-fgac", "hive", "INSERT_OVERWRITE"): {
        "kind": "new_failure",
        "table_type": "external_partitioned",
        "error": (
            "java.nio.file.AccessDeniedException: s3://etd-example-111122223333/lf-testbed/lf_test_db/: "
            "getFileStatus on s3://etd-example-111122223333/lf-testbed/lf_test_db/: "
            "com.amazonaws.services.s3.model.AmazonS3Exception: Access Denied (Service: Amazon S3; "
            "Status Code: 403; Request ID: 7QK3PE9V2XJ1MZ4A; Extended Request ID: n1p8...); "
            "no identity-based policy allows the s3:ListBucket action on the parent prefix. "
            "Hive staging mkdirs walks the database prefix; the Lake Formation vended session is scoped "
            "to the table prefix only."
        ),
        "note": (
            "Appears only at/after EMR 7.12, where DML/DDL that modify table data switched from "
            "runtime-role credentials to Lake Formation vended credentials."
        ),
    },
    ("v-713-fgac", "delta", "INSERT_INTO"): {
        "kind": "silent_data_loss",
        "table_type": "managed_partitioned",
        "note": (
            "Job exits 0. Data files land on S3 but the Delta commit log does not advance, so readers "
            "still see the previous snapshot."
        ),
    },
    ("v-711-fgac", "hive", "DF_WRITER_V1"): {"kind": "orphaned_data", "table_type": "managed"},
    ("v-713-fgac", "hive", "DF_WRITER_V1"): {"kind": "orphaned_data", "table_type": "managed"},
    ("v-713-fgac-p1", "hive", "DF_WRITER_V1"): {"kind": "orphaned_data", "table_type": "managed"},
    ("v-713-fgac", "iceberg", "MERGE_INTO"): {"kind": "flaky", "table_type": "managed"},
    ("v-713-fgac-p1", "iceberg", "MERGE_INTO"): {"kind": "flaky", "table_type": "managed"},
}

MODE_OVERHEAD = {"plain": 1.00, "lf_fta": 1.08, "lf_fgac": 1.42}


def functional_units(variant: dict, matrix: dict, iterations: int) -> list[dict]:
    units: list[dict] = []
    overhead = MODE_OVERHEAD[variant["access_mode"]]
    # 7.11 FGAC carried slightly more record-server overhead than 7.13.
    if variant["access_mode"] == "lf_fgac" and not ge(variant["release_label"], "7.12.0"):
        overhead *= 1.02

    for fmt, ops in matrix["formats"].items():
        for op, entry in ops.items():
            state, why = expected_state(fmt, op, entry, variant)
            defect = DEFECTS.get((variant["variant_id"], fmt, op))
            seed = f"{variant['variant_id']}|{fmt}|{op}"
            table_type = (defect or {}).get("table_type") or pick(seed, TABLE_TYPES)
            base = jitter(seed + "|dur", 2.5, 26.0)
            duration = round(base * overhead, 2)
            expected_rows = 5000

            unit = {
                "name": op,
                "table_format": fmt,
                "table_type": table_type,
                "expected_state": state,
                "expected_reason": why,
                "lf_permissions": entry.get("lf", []),
                "duration_s": duration,
                "iterations_observed": iterations,
                "job_id": "0000" + hashlib.sha256(seed.encode()).hexdigest()[:12],
            }

            if state == "NA":
                unit.update({"status": "NOT_APPLICABLE", "error": None, "row_count": None,
                             "result_checksum": None})
                units.append(unit)
                continue

            if state == "N":
                unit.update({
                    "status": "FAILED",
                    "error": f"Operation {op} on {fmt} is not supported: {why}",
                    "row_count": None,
                    "result_checksum": None,
                })
                units.append(unit)
                continue

            # Expected to work.
            if defect and defect["kind"] == "new_failure":
                unit.update({"status": "FAILED", "error": defect["error"], "row_count": None,
                             "result_checksum": None, "defect_note": defect["note"]})
            elif defect and defect["kind"] == "silent_data_loss":
                unit.update({
                    "status": "SUCCESS",
                    "error": None,
                    "row_count": 3200,
                    "result_checksum": checksum(seed + "|divergent"),
                    "table_version_advanced": False,
                    "defect_note": defect["note"],
                })
            elif defect and defect["kind"] == "orphaned_data":
                unit.update({
                    "status": "SUCCESS",
                    "error": None,
                    "row_count": expected_rows,
                    "result_checksum": checksum(f"canon|{fmt}|{op}"),
                    "orphaned_object_count": 48,
                    "orphaned_bytes": 191_365_120,
                    "defect_note": "Overwrite could not delete prior-generation files; new files written alongside old.",
                })
            elif defect and defect["kind"] == "flaky":
                unit.update({
                    "status": "FLAKY",
                    "error": "org.apache.iceberg.exceptions.CommitFailedException: "
                             "Cannot commit: stale table metadata (iteration 2 of 2 only)",
                    "row_count": expected_rows,
                    "result_checksum": checksum(f"canon|{fmt}|{op}"),
                    "iteration_statuses": ["SUCCESS", "FAILED"],
                })
            else:
                unit.update({
                    "status": "SUCCESS",
                    "error": None,
                    "row_count": expected_rows,
                    "result_checksum": checksum(f"canon|{fmt}|{op}"),
                    "table_version_advanced": True,
                })
            units.append(unit)
    return units


# ------------------------------------------------------------------ perf units

QUERIES = ["q1", "q3", "q4", "q7", "q11", "q17", "q19", "q23a", "q25", "q28",
           "q34", "q42", "q50", "q59", "q64", "q67", "q72", "q75", "q78", "q95"]

# Baseline seconds on the *plain* variant, before access-mode overhead.
PLAIN_BASE = {
    "q1": 11.8, "q3": 6.2, "q4": 74.5, "q7": 9.4, "q11": 58.1, "q17": 21.6,
    "q19": 8.9, "q23a": 66.3, "q25": 24.7, "q28": 39.2, "q34": 12.1, "q42": 4.8,
    "q50": 27.9, "q59": 33.4, "q64": 81.6, "q67": 96.2, "q72": 118.4, "q75": 44.0,
    "q78": 62.7, "q95": 51.3,
}

# Per-query multipliers applied to the 7.13 FGAC variant only — the upgrade story.
UPGRADE_FACTOR = {
    "q23a": 1.40, "q72": 1.24, "q95": 1.16,   # regressions
    "q4": 0.81, "q64": 0.90,                   # improvements
}

TIMEOUT_ON = {("v-713-fgac", "q78"), ("v-713-fgac-p1", "q78")}


def perf_units(variant: dict, iterations: int) -> list[dict]:
    mode_mult = MODE_OVERHEAD[variant["access_mode"]]
    is_711 = not ge(variant["release_label"], "7.12.0")
    if variant["access_mode"] == "lf_fgac" and is_711:
        mode_mult *= 1.02

    units = []
    for q in QUERIES:
        base = PLAIN_BASE[q] * mode_mult
        if variant["access_mode"] == "lf_fgac" and not is_711:
            base *= UPGRADE_FACTOR.get(q, 1.0)

        if (variant["variant_id"], q) in TIMEOUT_ON:
            units.append({
                "name": q, "status": "TIMEOUT", "iterations": [],
                "error": "Job did not reach a terminal state within 1800s",
                "job_id": "0000" + hashlib.sha256(f"{variant['variant_id']}{q}".encode()).hexdigest()[:12],
            })
            continue

        iters = []
        for i in range(iterations):
            f = jitter(f"{variant['variant_id']}|{q}|{i}", 0.985, 1.045)
            iters.append(round(base * f, 2))
        units.append({
            "name": q,
            "status": "SUCCESS",
            "iterations": iters,
            "row_count": 100,
            "error": None,
            "job_id": "0000" + hashlib.sha256(f"{variant['variant_id']}{q}".encode()).hexdigest()[:12],
        })
    return units


# ------------------------------------------------------------------ cost facts


def cost_facts(variant: dict, wall_clock_s: float, avg_executors: int = 12) -> dict:
    sh = variant["shape"]
    drivers = sh.get("drivers_per_job", 1)
    vcpu = sh["driver_cores"] * drivers + sh["executor_cores"] * avg_executors
    mem_gb = int(sh["driver_memory"].rstrip("g")) * drivers + int(sh["executor_memory"].rstrip("g")) * avg_executors
    hours = wall_clock_s / 3600.0
    return {
        "vcpu_hour": round(vcpu * hours, 4),
        "memory_gb_hour": round(mem_gb * hours, 4),
        "storage_gb_hour": round(20 * avg_executors * hours, 4),
        "wall_clock_s": round(wall_clock_s, 1),
        "drivers_per_job": drivers,
        "avg_executors": avg_executors,
        "source": "synthetic fixture — derived from shape x wall clock",
    }


# ----------------------------------------------------------------------- main


# ---------------------------------------------------------- data filter units
#
# Three Lake Formation data cell filters per FGAC variant. The candidate in the
# patch-validation pair is given an unenforced row filter so the report has a
# FILTER_NOT_ENFORCED finding to render: a granted filter that returns the whole
# table is a disclosure, and it is the one failure mode that looks like success
# from inside the job.

FILTER_UNITS = [
    ("ROW_FILTER", "row", 1000, 1000, ["amount", "category", "dim_id", "fact_id"],
     None, True, None),
    ("COLUMN_FILTER", "column", 10000, 10000, ["category", "dim_id", "fact_id"],
     ["category", "dim_id", "fact_id"], None, True),
    ("CELL_FILTER", "cell", 1000, 1000, ["category", "fact_id"],
     ["category", "fact_id"], True, True),
]


def filter_units(variant: dict, matrix: dict, unenforced: bool) -> list:
    variant_id = variant["variant_id"]
    out = []
    for (op, kind, vis, permitted, cols, want, row_ok, col_ok) in FILTER_UNITS:
        # The unenforced case: every row comes back despite a row filter.
        bad = unenforced and kind in ("row", "cell")
        rows_visible = 10000 if bad else vis
        rec = {
            # Filters are defined on the fact table, so the check is not
            # format-specific and is no longer labelled per format.
            "name": op, "table_format": "n/a", "table_type": "managed",
            "status": "FAILED" if bad else "SUCCESS",
            "error": (f"AssertionError: data filter '{kind}' not enforced: "
                      f"{rows_visible} rows visible, {permitted} permitted"
                      if bad else None),
            "duration_s": round(jitter(f"{variant_id}{op}", 0.4, 2.5), 2),
            "row_count": rows_visible,
            "result_checksum": checksum(f"{variant_id}{op}"),
            "filter_kind": kind,
            "rows_visible": rows_visible,
            "rows_permitted": permitted,
            "columns_visible": sorted(cols),
            "columns_permitted": sorted(want) if want else None,
            "row_filter_enforced": (False if bad else row_ok),
            "column_filter_enforced": col_ok,
            "filter_over_disclosure": bad,
        }
        # Resolved from the same matrix as every other unit, so a filter op is
        # judged against documented support rather than against a hardcoded
        # assumption.
        rec["expected_state"] = "S"
        rec["expected_reason"] = ("data cell filter granted to a non-administrator "
                                  "reader principal")
        out.append(rec)
    return out


def main() -> None:
    manifest = json.loads((FIX / "run_manifest.json").read_text())
    # The canonical matrix, not a copy. A duplicate under fixtures/ went missing
    # at some point and this generator could not run at all; one source of truth
    # means the fixtures cannot drift from what the tool actually asserts.
    matrix = json.loads(
        (pathlib.Path(__file__).resolve().parents[2] / "etd/matrices/lf_fgac.json").read_text())
    UNITS.mkdir(parents=True, exist_ok=True)

    workloads = {w["workload_id"]: w for w in manifest["workloads"]}
    written = []

    for variant in manifest["variants"]:
        # functional
        w = workloads["func/lf-fgac-core"]
        units = functional_units(variant, matrix, w["iterations"])
        # Filter enforcement is only assertable under FGAC. The patched candidate
        # carries an unenforced filter so the report has the finding to render.
        if variant["access_mode"] == "lf_fgac":
            units += filter_units(
                variant, matrix,
                unenforced=variant["variant_id"].endswith("-p1"))
        wall = sum(u["duration_s"] for u in units) + 95  # + startup
        payload = {
            "run_id": manifest["run_id"],
            "variant_id": variant["variant_id"],
            "workload_id": w["workload_id"],
            "unit_kind": w["unit_kind"],
            "iterations": w["iterations"],
            "cost_facts": cost_facts(variant, wall, avg_executors=6),
            "data_class": "SAMPLE",
            "units": units,
        }
        p = UNITS / f"{variant['variant_id']}__func-lf-fgac-core.json"
        p.write_text(json.dumps(payload, indent=2) + "\n")
        written.append((p.name, len(units)))

        # perf
        w = workloads["perf/tpcds-sf100"]
        punits = perf_units(variant, w["iterations"])
        best = sum(min(u["iterations"]) for u in punits if u["iterations"])
        wall = best * w["iterations"] + 120
        payload = {
            "run_id": manifest["run_id"],
            "variant_id": variant["variant_id"],
            "workload_id": w["workload_id"],
            "unit_kind": w["unit_kind"],
            "iterations": w["iterations"],
            "cost_facts": cost_facts(variant, wall, avg_executors=12),
            "data_class": "SAMPLE",
            "units": punits,
        }
        p = UNITS / f"{variant['variant_id']}__perf-tpcds-sf100.json"
        p.write_text(json.dumps(payload, indent=2) + "\n")
        written.append((p.name, len(punits)))

    for name, n in written:
        print(f"  wrote {name:52s} {n:3d} units")
    print(f"\n{len(written)} fixture files in {UNITS}")


if __name__ == "__main__":
    main()

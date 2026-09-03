"""Expected-support matrices and the resolver that turns them into verdicts.

This is the piece that stops the tool crying wolf. AWS documents, per table
format and per operation, whether something is supported, supported only with
direct S3 IAM, or not supported at all — and some entries are gated on the EMR
release. Diffing observed results against *expectation* rather than against
pass/fail is what makes a documented "not supported" report as
EXPECTED_UNSUPPORTED instead of as a regression.
"""

from __future__ import annotations

import functools
import json
from pathlib import Path

SUPPORT_DIR = Path(__file__).parent / "matrices"

# The harness runs a few operations under longer names than the documentation
# tables use.
# A table created with `USING parquet` is a Spark v1 file-source table, which is
# the family the documentation tables call "hive". Without this alias every
# parquet operation resolves to UNKNOWN under the Lake Formation matrices.
FORMAT_ALIASES = {"parquet": "hive", "csv": "hive", "orc": "hive", "avro": "hive"}

ALIASES = {
    "ALTER_TABLE_ADD_COLUMN": "ALTER_TABLE",
    "ALTER_TABLE_ADD_COLUMNS": "ALTER_TABLE",
    "INSERT": "INSERT_INTO",
    "SAVE_AS_TABLE_APPEND": "DF_WRITER_V2",
}


# A catalog choice is not an access-control mode: with an external Hive
# metastore, Lake Formation is not in the path, so Spark's operation support is
# the same as with the Glue Data Catalog. Aliasing avoids a duplicate matrix that
# would drift.
MATRIX_ALIASES = {"hms": "plain"}


@functools.lru_cache(maxsize=8)
def load_matrix(access_mode: str) -> dict:
    access_mode = MATRIX_ALIASES.get(access_mode, access_mode)
    p = SUPPORT_DIR / f"{access_mode}.json"
    if not p.exists():
        raise FileNotFoundError(
            f"No expected-support matrix for access_mode={access_mode!r}. "
            f"Expected {p}. Available: {sorted(x.stem for x in SUPPORT_DIR.glob('*.json'))}")
    return json.loads(p.read_text())


def _release_tuple(release_label: str) -> tuple[int, ...]:
    core = release_label.replace("emr-", "").replace("spark-", "")
    parts = []
    for p in core.split("."):
        digits = "".join(ch for ch in p if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts) or (0,)


def _ge(release_label: str, gate: str) -> bool:
    return _release_tuple(release_label) >= _release_tuple(gate)


def expected_state(access_mode: str, table_format: str, operation: str,
                   release_label: str) -> tuple[str, str]:
    """Return (state, reason). State is S | S3 | N | NA | UNKNOWN."""
    op = ALIASES.get(operation, operation)
    try:
        matrix = load_matrix(access_mode)
    except FileNotFoundError as exc:
        return "UNKNOWN", str(exc)

    formats = matrix.get("formats") or {}
    fmt_entry = formats.get(table_format)
    if fmt_entry is None and table_format in FORMAT_ALIASES:
        table_format = FORMAT_ALIASES[table_format]
        fmt_entry = formats.get(table_format)
    if fmt_entry is None:
        return "UNKNOWN", f"no expectation recorded for format {table_format!r} in {access_mode}"
    entry = fmt_entry.get(op)
    if entry is None:
        return "UNKNOWN", f"no expectation recorded for {table_format}.{op} in {access_mode}"

    gates = matrix.get("version_gates") or {}
    gate = entry.get("min_release")
    if gate and not _ge(release_label, gate):
        return "N", f"not supported before EMR {gate} (documented)"

    dum = gates.get("delete_update_merge_unsupported_at_or_before")
    if dum and op in ("DELETE", "UPDATE", "MERGE_INTO") and not _ge(release_label, dum):
        # e.g. FGAC: 7.11 and older do not support DELETE/UPDATE/MERGE at all.
        if _release_tuple(release_label) <= _release_tuple(dum):
            return "N", (f"EMR {dum} and older do not support DELETE/UPDATE/MERGE "
                         f"under {access_mode} (documented)")

    return entry["state"], entry.get("caveat", "documented support state")


def lf_permissions(access_mode: str, table_format: str, operation: str) -> list[str]:
    op = ALIASES.get(operation, operation)
    try:
        formats = load_matrix(access_mode)["formats"]
        if table_format not in formats:
            table_format = FORMAT_ALIASES.get(table_format, table_format)
        entry = formats.get(table_format, {}).get(op) or {}
    except FileNotFoundError:
        return []
    return list(entry.get("lf") or [])


def matrix_provenance(access_mode: str) -> dict:
    try:
        m = load_matrix(access_mode)
    except FileNotFoundError:
        return {}
    return {"access_mode": access_mode, "sources": m.get("sources", []),
            "retrieved": m.get("retrieved"), "basis": m.get("basis")}

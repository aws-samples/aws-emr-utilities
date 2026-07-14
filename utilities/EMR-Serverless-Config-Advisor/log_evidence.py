#!/usr/bin/env python3
"""Driver/executor stderr signature extraction for the Config Advisor UI.

The event log records WHAT happened (spill, task failure, executor lost);
stderr records WHY (heap OOM vs cgroup kill vs disk full vs S3 throttling).
This module scans an uploaded zip of driver/executor logs against a curated
signature table and returns structured evidence that refines — never
contradicts — the event-log-based analysis.

Curated regexes only: unknown lines produce no signal rather than a wrong
one, which keeps the output stable across Spark/EMR releases.
"""

import gzip
import io
import re
import zipfile
from pathlib import PurePosixPath

# Hard caps so a pathological upload can't stall the request thread
MAX_FILES = 400            # driver + failed executors, not a 400-worker fleet
MAX_BYTES_PER_FILE = 256 * 1024 * 1024
MAX_EXCERPTS_PER_SIG = 5
EXCERPT_CONTEXT_LINES = 4  # lines kept after a match (stack trace head)
EXCERPT_MAX_CHARS = 1600

# Each signature: what to look for, what it means, and which knob it moves.
# `refines` names the bottleneck dimension the evidence corroborates so the
# results page can annotate the classifier's verdict without re-scoring it.
SIGNATURES = [
    {
        "id": "heap_oom",
        "pattern": re.compile(r"java\.lang\.OutOfMemoryError(?!:\s*GC overhead)"
                              r"(?::\s*[^\n]{0,80})?"),
        "title": "JVM heap OutOfMemoryError",
        "meaning": "Executor (or driver) JVM heap exhausted — raise "
                   "spark.executor.memory / spark.driver.memory, not overhead.",
        "refines": "Memory Constrained",
    },
    {
        "id": "gc_overhead",
        "pattern": re.compile(r"java\.lang\.OutOfMemoryError:\s*GC overhead limit exceeded"),
        "title": "GC overhead limit exceeded",
        "meaning": "Heap technically not exhausted but GC is thrashing — "
                   "raise executor memory or cut per-task footprint "
                   "(fewer cores per executor, smaller partitions).",
        "refines": "Memory Constrained",
    },
    {
        "id": "container_rss_kill",
        "pattern": re.compile(
            r"(killed by YARN for exceeding (?:physical )?memory limits"
            r"|Container killed on request\. Exit code is 137"
            r"|exceeding memory limits[^\n]{0,120}"
            r"|Out of memory: Killed process)"),
        "title": "Container killed by memory limit (RSS/cgroup)",
        "meaning": "Whole-container RSS breached the limit — off-heap/native/"
                   "Python memory, not JVM heap. Raise "
                   "spark.emr-serverless.memoryOverheadFactor (or "
                   "memoryOverhead), not executor.memory.",
        "refines": "Memory Constrained",
    },
    {
        "id": "disk_full",
        "pattern": re.compile(r"No space left on device"),
        "title": "Local disk full",
        "meaning": "Shuffle/spill filled local disk — raise "
                   "spark.emr-serverless.executor.disk or use "
                   "disk.type=shuffle_optimized.",
        "refines": "IOPS Bound",
    },
    {
        "id": "fetch_failed",
        "pattern": re.compile(r"FetchFailed(?:Exception)?[^\n]{0,100}"),
        "title": "Shuffle fetch failure",
        "meaning": "Executor could not fetch shuffle blocks — usually the "
                   "serving executor died (check for OOM/kill signatures) or "
                   "network timeout; consider fewer, larger executors and "
                   "spark.network.timeout.",
        "refines": "Network Bound",
    },
    {
        "id": "s3_throttle",
        "pattern": re.compile(r"(Slow Down|503 Slow ?Down|ThrottlingException"
                              r"|Please reduce your request rate)"),
        "title": "S3 throttling (503 Slow Down)",
        "meaning": "S3 request-rate throttling — slow tasks are retry storms, "
                   "not compute. Tune partition sizes / "
                   "fs.s3.maxRetries+backoff instead of adding workers.",
        "refines": "IOPS Bound",
    },
    {
        "id": "driver_max_result",
        "pattern": re.compile(r"bigger than spark\.driver\.maxResultSize[^\n]{0,80}"
                              r"|Total size of serialized results[^\n]{0,120}"),
        "title": "Driver maxResultSize exceeded",
        "meaning": "collect()/broadcast pulled too much to the driver — raise "
                   "spark.driver.maxResultSize and driver memory, or avoid "
                   "the collect.",
        "refines": None,
    },
    {
        "id": "broadcast_timeout",
        "pattern": re.compile(r"Could not execute broadcast in \d+ secs"
                              r"|broadcastTimeout"),
        "title": "Broadcast timeout",
        "meaning": "Broadcast join side too large or driver overloaded — "
                   "raise spark.sql.broadcastTimeout or lower "
                   "spark.sql.autoBroadcastJoinThreshold.",
        "refines": None,
    },
    {
        "id": "task_memory_contention",
        "pattern": re.compile(r"TaskMemoryManager[^\n]{0,40}"
                              r"(Failed to acquire|Failed to allocate)[^\n]{0,80}"),
        "title": "Execution-memory contention (TaskMemoryManager)",
        "meaning": "Concurrent tasks contending for execution memory forces "
                   "early spill — fewer cores per executor (e.g. 8→4) "
                   "relieves it even when total memory looks sufficient.",
        "refines": "Memory Constrained",
    },
    {
        "id": "python_oom",
        "pattern": re.compile(r"((?<![a-zA-Z])MemoryError(?![a-zA-Z])"
                              r"|Python worker exited unexpectedly"
                              r"|pyspark\.errors[^\n]{0,60}MemoryError)"),
        "title": "Python worker memory failure",
        "meaning": "PySpark worker (off-heap) ran out of memory — raise "
                   "memoryOverheadFactor or spark.executor.pyspark.memory; "
                   "heap settings won't help.",
        "refines": "Memory Constrained",
    },
    {
        "id": "executor_lost",
        "pattern": re.compile(r"ExecutorLostFailure \(executor \d+ exited[^\n]{0,120}"),
        "title": "Executor lost",
        "meaning": "Executors exiting mid-stage — correlate with OOM/kill/disk "
                   "signatures above to find the root cause; casualties "
                   "re-run work and inflate cost.",
        "refines": None,
    },
    {
        "id": "kill_signal",
        "pattern": re.compile(r"(Executor is exiting due to.{0,80}"
                              r"|RECEIVED SIGNAL TERM|SIGTERM|SIGKILL)"),
        "title": "Executor received kill signal",
        "meaning": "External termination (scale-in, cgroup kill, host issue) — "
                   "if paired with memory-limit signatures it's an OOM kill; "
                   "alone it's usually dynamic-allocation churn (benign).",
        "refines": None,
    },
]

# Log-file layouts we auto-detect inside the zip. Anything not matching is
# still scanned if it looks like a log file, tagged source="unknown".
_DRIVER_PAT = re.compile(r"(SPARK_DRIVER/|driver[^/]*\.(log|txt)(\.gz)?$"
                         r"|container_\d+_\d+_\d+_000001/)", re.I)
_EXEC_ID_PATS = (
    re.compile(r"SPARK_EXECUTOR/(?P<id>[^/]+)/", re.I),          # EMR Serverless
    re.compile(r"container_(?:\w+_)?\d+_\d+_\d+_(?P<id>\d{6})/"),  # YARN
    re.compile(r"executor[-_](?P<id>\d+)", re.I),                 # loose naming
)
_LOGLIKE = re.compile(r"(std(err|out)|\.log|\.txt)(\.gz)?$", re.I)
_SKIP = re.compile(r"(__MACOSX/|\.DS_Store$|events?_\d|eventlog|appstatus"
                   r"|\.json$|\.crc$)", re.I)


def _classify_member(name: str) -> tuple:
    """(source, executor_id) for a zip member path; source in
    driver|executor|unknown."""
    if _DRIVER_PAT.search(name):
        return "driver", None
    for pat in _EXEC_ID_PATS:
        m = pat.search(name)
        if m:
            return "executor", m.group("id").lstrip("0") or "0"
    return "unknown", None


def _iter_log_members(zf: zipfile.ZipFile):
    """Yield (info, source, exec_id) for scannable log files in the zip."""
    count = 0
    for info in zf.infolist():
        if info.is_dir() or _SKIP.search(info.filename):
            continue
        if not _LOGLIKE.search(info.filename):
            continue
        if info.file_size > MAX_BYTES_PER_FILE:
            continue
        source, exec_id = _classify_member(info.filename)
        yield info, source, exec_id
        count += 1
        if count >= MAX_FILES:
            return


def _open_member(zf: zipfile.ZipFile, info: zipfile.ZipInfo):
    """Text stream over a member, transparently gunzipping .gz."""
    raw = zf.open(info)
    if info.filename.endswith(".gz"):
        raw = gzip.GzipFile(fileobj=raw)
    return io.TextIOWrapper(raw, encoding="utf-8", errors="replace")


def _scan_stream(stream, source, exec_id, filename, hits):
    """Scan one log stream; accumulate counts and capped excerpts into
    hits[sig_id]. Streams line-by-line — never loads the file. Context
    capture runs alongside matching so a stack trace being excerpted can't
    hide other signatures on the same lines."""
    pending = []  # [sig_id, lines_remaining, buffer] per open excerpt
    for line in stream:
        line = line.rstrip("\n")
        for p in pending:
            p[2].append(line)
            p[1] -= 1
        done = [p for p in pending if p[1] <= 0 or not line.strip()]
        for p in done:
            _flush_excerpt(hits[p[0]], p[2], source, exec_id, filename)
        pending = [p for p in pending if p not in done]

        for sig in SIGNATURES:
            if sig["pattern"].search(line):
                h = hits[sig["id"]]
                h["count"] += 1
                key = (source, exec_id)
                h["sources"][key] = h["sources"].get(key, 0) + 1
                if (len(h["excerpts"]) < MAX_EXCERPTS_PER_SIG
                        and not any(p[0] == sig["id"] for p in pending)):
                    # capture the matched line + a few follow lines (stack head)
                    pending.append([sig["id"], EXCERPT_CONTEXT_LINES, [line]])
                break  # first matching signature wins for a line
    for p in pending:
        _flush_excerpt(hits[p[0]], p[2], source, exec_id, filename)


def _flush_excerpt(hit, buf, source, exec_id, filename):
    text = "\n".join(buf)[:EXCERPT_MAX_CHARS]
    hit["excerpts"].append({
        "source": source + (f" {exec_id}" if exec_id else ""),
        "file": PurePosixPath(filename).name,
        "text": text,
    })


def extract_log_evidence(zip_path) -> dict:
    """Scan a zip of driver/executor logs; return structured evidence.

    Returns {"signatures": [...], "files_scanned": n, "files_skipped": n,
    "sources": {"driver": n, "executor": n, "unknown": n}} — signatures
    sorted by severity of what they explain (order of SIGNATURES table),
    each with count, per-source counts, and capped excerpts.
    """
    hits = {s["id"]: {"count": 0, "sources": {}, "excerpts": []}
            for s in SIGNATURES}
    files_scanned = 0
    source_counts = {"driver": 0, "executor": 0, "unknown": 0}

    with zipfile.ZipFile(zip_path) as zf:
        members = list(_iter_log_members(zf))
        files_skipped = sum(
            1 for i in zf.infolist()
            if not i.is_dir() and not _SKIP.search(i.filename)) - len(members)
        for info, source, exec_id in members:
            try:
                with _open_member(zf, info) as stream:
                    _scan_stream(stream, source, exec_id, info.filename, hits)
                files_scanned += 1
                source_counts[source] += 1
            except Exception:
                files_skipped += 1

    signatures = []
    for sig in SIGNATURES:
        h = hits[sig["id"]]
        if not h["count"]:
            continue
        signatures.append({
            "id": sig["id"],
            "title": sig["title"],
            "meaning": sig["meaning"],
            "refines": sig["refines"],
            "count": h["count"],
            "sources": {f"{src}{' ' + eid if eid else ''}": n
                        for (src, eid), n in sorted(
                            h["sources"].items(),
                            key=lambda kv: -kv[1])[:10]},
            "excerpts": h["excerpts"],
        })

    return {
        "signatures": signatures,
        "files_scanned": files_scanned,
        "files_skipped": max(files_skipped, 0),
        "sources": source_counts,
    }


def corroboration_notes(evidence: dict, bottleneck: dict) -> list:
    """Notes linking log signatures to the event-log bottleneck verdict.

    Additive only: log evidence sharpens the classifier's diagnosis (e.g.
    'Memory Constrained' → 'heap OOM, raise executor.memory') but never
    overrides or re-scores it.
    """
    notes = []
    primary = (bottleneck or {}).get("primary", "")
    for sig in evidence.get("signatures", []):
        if sig["refines"] and sig["refines"] == primary:
            notes.append(
                f"Log evidence corroborates the {primary} verdict: "
                f"{sig['title']} ({sig['count']}×). {sig['meaning']}")
    return notes


if __name__ == "__main__":
    import json
    import sys
    print(json.dumps(extract_log_evidence(sys.argv[1]), indent=2))

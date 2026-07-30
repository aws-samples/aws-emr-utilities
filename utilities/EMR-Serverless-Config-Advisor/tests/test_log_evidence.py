#!/usr/bin/env python3
"""Unit tests for log_evidence.py — curated stderr signature extraction.

Builds synthetic zips in-memory covering both log layouts (EMR Serverless
SPARK_DRIVER/SPARK_EXECUTOR and YARN container_*) plus noise files, and
asserts every signature in the table fires on its canonical line and does
not fire on look-alikes.

Usage: python3 tests/test_log_evidence.py
Exit codes: 0 = pass, 1 = failure.
"""
import gzip
import io
import os
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from log_evidence import (SIGNATURES, corroboration_notes,
                          extract_log_evidence, scan_text)

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


def make_zip(members: dict) -> str:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, text in members.items():
            data = text.encode()
            if name.endswith(".gz"):
                data = gzip.compress(data)
            z.writestr(name, data)
    import tempfile
    f = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    f.write(buf.getvalue())
    f.close()
    return f.name


# One canonical stderr line per signature id
CANONICAL = {
    "heap_oom": "26/07/09 ERROR Executor: java.lang.OutOfMemoryError: Java heap space",
    "gc_overhead": "java.lang.OutOfMemoryError: GC overhead limit exceeded",
    "container_rss_kill": "Container killed on request. Exit code is 137",
    "disk_full": "java.io.IOException: No space left on device",
    "fetch_failed": "org.apache.spark.shuffle.FetchFailedException: Failed to connect to host:7337",
    "s3_throttle": "Please reduce your request rate. (Status Code: 503; Error Code: SlowDown)",
    "driver_max_result": "Total size of serialized results of 15 tasks (2.1 GiB) is bigger than spark.driver.maxResultSize (1024.0 MiB)",
    "broadcast_timeout": "Could not execute broadcast in 300 secs.",
    "task_memory_contention": "26/07/09 ERROR TaskMemoryManager: Failed to acquire 1048576 bytes of memory, got 0",
    "python_oom": "26/07/09 ERROR PythonRunner: MemoryError",
    "executor_lost": "ExecutorLostFailure (executor 628 exited caused by one of the running tasks) Reason: Unknown executor exit code (0)",
    "kill_signal": "26/07/09 ERROR CoarseGrainedExecutorBackend: RECEIVED SIGNAL TERM",
}

# Lines that pattern-match naively but must NOT fire the named signature
NEGATIVES = {
    "heap_oom": "java.lang.OutOfMemoryError: GC overhead limit exceeded",
    "s3_throttle": "NettyBlockTransferService' on port 42503.",
    "python_oom": "ignoring OutOfMemoryErrors from foo",
}


def test_signature_table():
    print("signature table: every id fires on its canonical line")
    for sig in SIGNATURES:
        line = CANONICAL[sig["id"]]
        first_match = next((s["id"] for s in SIGNATURES
                            if s["pattern"].search(line)), None)
        check(f"{sig['id']} fires first", first_match == sig["id"],
              f"(got {first_match})")

    print("signature table: negatives don't fire")
    for sig_id, line in NEGATIVES.items():
        sig = next(s for s in SIGNATURES if s["id"] == sig_id)
        check(f"{sig_id} negative", not sig["pattern"].search(line)
              or next(s["id"] for s in SIGNATURES
                      if s["pattern"].search(line)) != sig_id)


def test_layouts_and_scan():
    print("zip scan: layouts, noise skipping, counting, excerpts")
    zp = make_zip({
        "SPARK_DRIVER/stderr.gz":
            CANONICAL["heap_oom"] + "\n\tat org.apache.spark.Foo.bar(Foo.java:1)\n"
            + CANONICAL["driver_max_result"] + "\n",
        "SPARK_EXECUTOR/12/stderr.gz":
            CANONICAL["disk_full"] + "\n" + CANONICAL["disk_full"] + "\n",
        "app_1/container_1770841089578_0001_01_000042/stderr":
            CANONICAL["container_rss_kill"] + "\n",
        # noise: must be skipped entirely
        "eventlog_v2_00gx/events_1_00gx": '{"Event":"SparkListenerLogStart"}\n',
        "__MACOSX/._junk": "x",
        "extract.json": "{}",
    })
    ev = extract_log_evidence(zp)
    os.unlink(zp)

    check("3 files scanned", ev["files_scanned"] == 3, f"(got {ev['files_scanned']})")
    check("driver detected", ev["sources"]["driver"] == 1)
    check("executors detected", ev["sources"]["executor"] == 2)
    ids = {s["id"]: s for s in ev["signatures"]}
    check("heap_oom found", "heap_oom" in ids)
    check("disk_full counted twice", ids.get("disk_full", {}).get("count") == 2)
    check("yarn exec id parsed",
          "executor 42" in ids.get("container_rss_kill", {}).get("sources", {}))
    check("excerpt keeps stack head",
          "Foo.java:1" in ids["heap_oom"]["excerpts"][0]["text"])
    check("event log not scanned",
          all("events_1" not in e["file"]
              for s in ev["signatures"] for e in s["excerpts"]))


def test_corroboration():
    print("corroboration notes: additive, verdict-matched only")
    zp = make_zip({"SPARK_EXECUTOR/1/stderr.gz":
                   CANONICAL["heap_oom"] + "\n" + CANONICAL["disk_full"] + "\n"})
    ev = extract_log_evidence(zp)
    os.unlink(zp)
    mem = corroboration_notes(ev, {"primary": "Memory Constrained"})
    iops = corroboration_notes(ev, {"primary": "IOPS Bound"})
    cpu = corroboration_notes(ev, {"primary": "CPU Constrained"})
    check("memory verdict gets heap note",
          any("OutOfMemoryError" in n for n in mem))
    check("iops verdict gets disk note",
          any("disk full" in n.lower() for n in iops))
    check("cpu verdict gets no notes", cpu == [])
    check("notes never re-score",
          all("corroborates" in n for n in mem + iops))


def test_scan_text():
    print("scan_text: zip-free path used by check_job_health")
    text = (CANONICAL["heap_oom"]
            + "\n\tat org.apache.spark.Foo.bar(Foo.java:1)\n"
            + CANONICAL["disk_full"] + "\n" + CANONICAL["disk_full"] + "\n")
    sigs = scan_text(text)
    ids = {s["id"]: s for s in sigs}
    check("heap_oom found", "heap_oom" in ids)
    check("disk_full counted twice", ids.get("disk_full", {}).get("count") == 2)
    check("source tagged driver",
          ids["heap_oom"]["sources"].get("driver") == 1)
    check("excerpt keeps stack head",
          "Foo.java:1" in ids["heap_oom"]["excerpts"][0]["text"])
    check("clean text yields no signatures",
          scan_text("26/07/24 INFO SparkContext: Running Spark 3.5.6\n") == [])


def test_empty_and_clean():
    print("edge cases: no log files / clean logs")
    zp = make_zip({"random.json": "{}"})
    ev = extract_log_evidence(zp)
    os.unlink(zp)
    check("no scannable files", ev["files_scanned"] == 0)

    zp = make_zip({"SPARK_DRIVER/stderr.gz":
                   "26/07/09 INFO SparkContext: Running Spark version 3.5.6\n"})
    ev = extract_log_evidence(zp)
    os.unlink(zp)
    check("clean log yields no signatures", ev["signatures"] == [])


if __name__ == "__main__":
    test_signature_table()
    test_layouts_and_scan()
    test_corroboration()
    test_scan_text()
    test_empty_and_clean()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): {FAILURES}")
        sys.exit(1)
    print("all log_evidence tests passed")

#!/usr/bin/env python3
"""Unit tests for 32-vCPU (XLarge) worker awareness.

Covers _preserve_xlarge_source and _xlarge_memory_from_heap with cases from
the field report that motivated the feature: a 32c/219G x12 TPC-DS run at
36% memory utilization was recommended Small 4c/27G x79, while the
proven-best manual fix kept 32c and cut memory to the 120 GB tier (108G
usable). 32c workers accept only three discrete memory configs (60/120/244
GB total -> 54/108/219 usable after the 10% overhead gap).

Usage: python3 tests/test_worker_32c.py
Exit codes: 0 = pass, 1 = failure.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from emr_s_fine_tuner import (_preserve_xlarge_source, _xlarge_memory_from_heap,
                              XLARGE_MEM_TIERS)

FAILURES = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name} {detail}")
    if not cond:
        FAILURES.append(name)


def preserve(is_ec2=False, orig_cores=32, spill_gb=0.0, fetch_wait=5.0,
             stages=None, peak_heap=84.07, orig_mem=219.0):
    return _preserve_xlarge_source(is_ec2, orig_cores, spill_gb, fetch_wait,
                                   stages or [], peak_heap, orig_mem)


def main():
    print("32c worker tests — _preserve_xlarge_source / _xlarge_memory_from_heap")

    # 1. Field-report scenario: 32c/219G source, 84 GB peak heap, no spill,
    #    healthy fetch-wait -> preserved on 32c at the 108G tier.
    r = preserve()
    check("field_report_preserved_108g",
          r == ("XLarge", {"vcpu": 32, "memory": 108}), f"(got {r})")

    # 2. Non-32c sources are never promoted (preservation only).
    for cores in (4, 8, 16, 48):
        check(f"no_promotion_{cores}c", preserve(orig_cores=cores) is None)

    # 3. EC2 sources excluded — EC2 32c boxes are not evidence the
    #    Serverless 32c shape works (different enforcement, disk, network).
    check("ec2_excluded", preserve(is_ec2=True) is None)

    # 4. Spill on the source: memory pressure — fall through to the
    #    escalation ladder rather than pinning the shape.
    check("spill_falls_through", preserve(spill_gb=500.0) is None)

    # 5. Shuffle-serving-bound source (high fetch-wait): the class where
    #    fewer fatter hosts lose (16c beat 32c 2.6x on an IOPS-bound agg).
    check("fetch_wait_falls_through", preserve(fetch_wait=45.0) is None)

    # 6. Retried stages: the run doesn't prove the 32c shape.
    stages = [{"stage_id": 1, "failure_reason": "FetchFailed"}]
    check("failed_stage_falls_through", preserve(stages=stages) is None)

    # 7. Memory tiers are the only legal values.
    check("tiers_are_discrete", XLARGE_MEM_TIERS == [54, 108, 219])

    # 8. Heap-based tier selection: 1.25x headroom on measured peak.
    #    <= 43.2 GB heap -> 54G; <= 86.4 -> 108G; above -> 219G.
    for heap, want in ((10, 54), (43, 54), (44, 108), (84.07, 108),
                       (86, 108), (87, 219), (200, 219), (500, 219)):
        got = _xlarge_memory_from_heap(heap, 219.0)
        check(f"heap_{heap}gb_to_{want}g", got == want, f"(got {got})")

    # 9. No heap measurement: keep the source's proven memory tier.
    check("no_heap_keeps_source_219", _xlarge_memory_from_heap(0, 219.0) == 219)
    check("no_heap_keeps_source_54", _xlarge_memory_from_heap(0, 54.0) == 54)
    check("no_heap_no_mem_max_tier", _xlarge_memory_from_heap(0, 0) == 219)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURES: {FAILURES}")
        return 1
    print("All 32c worker tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

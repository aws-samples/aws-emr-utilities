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
                              _promote_to_xlarge, XLARGE_MEM_TIERS)

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

    # --- Promotion: consolidate large small-worker fleets onto 32c ---
    print("\n32c promotion tests — _promote_to_xlarge")

    def promote(is_ec2=False, orig_cores=16, wtype="Small", wvcpu=4,
                spill=0.0, dspill=0.0, fetch_wait=0.0, stages=None,
                s_out_gb=677.0, max_exec=60, floor=0):
        return _promote_to_xlarge(is_ec2, orig_cores, wtype, wvcpu, spill,
                                  dspill, fetch_wait, stages or [],
                                  s_out_gb, max_exec, floor)

    # 10. Calibration case A (43-join multijoin replica, 32c proven at
    #     equal vCPU): Small x60 = 240 vCPU, 677 GB shuffle, clean run
    #     -> consolidates to ceil(240/32) = 8 XLarge workers.
    check("multijoin_promotes_to_8", promote() == 8)

    # 11. Calibration case B (IOPS-bound single-agg, 16c won 2.6x):
    #     each veto must fire INDEPENDENTLY.
    #     B as recommended: Medium x549, 4737 GB shuffle, fetch-wait 27%,
    #     serving floor 549 hosts.
    vb = dict(wtype="Medium", wvcpu=8, s_out_gb=4737.0, max_exec=549)
    check("iopsbound_fetchwait_veto", promote(fetch_wait=27.2, **vb) is None)
    check("iopsbound_serving_floor_veto", promote(floor=549, **vb) is None)
    check("iopsbound_both_vetoed", promote(fetch_wait=27.2, floor=549, **vb) is None)
    # Sanity: with both vetoes lifted the shape WOULD promote — proving
    # the vetoes (not some other gate) are what block this class.
    check("iopsbound_promotes_without_vetoes", promote(**vb) == 138)

    # 11b. Promotion fetch-wait veto is 10% — stricter than preservation's
    #      20% guard. The 10-20% band has no empirical coverage (validated:
    #      0% promotes and wins even at 8x block count; 27% amplified to
    #      75% on consolidation, 2.6x loss). Sources in the band keep
    #      today's recommendation; preservation keeps its 20% guard since
    #      its source already proved the 32c shape at that fetch-wait.
    check("band_fetchwait_15pct_no_promote", promote(fetch_wait=15.0) is None)
    check("band_fetchwait_11pct_no_promote", promote(fetch_wait=11.0) is None)
    check("fetchwait_10pct_promotes", promote(fetch_wait=10.0) == 8)
    check("preservation_keeps_20pct_guard",
          preserve(fetch_wait=15.0) is not None)

    # 12. Scale floor: below 192 total vCPU consolidation isn't worth it.
    check("small_fleet_no_promote", promote(max_exec=47) is None)   # 188 vCPU
    check("at_192_vcpu_promotes", promote(max_exec=48) == 6)        # benchmark shape

    # 13. Spill vetoes (execution-memory contention risk grows with
    #     cores per executor).
    check("mem_spill_veto", promote(spill=100.0) is None)
    check("disk_spill_veto", promote(dspill=100.0) is None)

    # 14. Retried stages veto.
    check("failed_stage_veto",
          promote(stages=[{"failure_reason": "FetchFailed"}]) is None)

    # 15. Shuffle-intensity floor: scan-heavy jobs with thin shuffle keep
    #     smaller workers for S3 read parallelism (field-report concern).
    check("thin_shuffle_no_promote", promote(s_out_gb=200.0) is None)  # 25 GB/worker

    # 16. Already 32c on Serverless (source or recommendation):
    #     preservation path owns it.
    check("already_32c_source", promote(orig_cores=32) is None)
    check("already_xlarge_rec", promote(wtype="XLarge", wvcpu=32) is None)

    # 17. EC2 sources ARE eligible: the scenario signature is
    #     workload-intrinsic, and the platform-dependent gates (spill,
    #     fetch-wait) transfer conservatively — EC2's lower mem/core
    #     OVERSTATES spill, so clean-on-EC2 is stronger evidence.
    check("ec2_clean_promotes", promote(is_ec2=True) == 8)
    # EC2 32-core source has no preservation path; consolidation
    # preserves the proven fat shape across the migration.
    check("ec2_32c_source_promotes", promote(is_ec2=True, orig_cores=32) == 8)
    # The typical EC2 migration profile (heavy spill from soft YARN
    # enforcement / low mem-per-core) is still vetoed by the spill gate.
    check("ec2_spilling_vetoed", promote(is_ec2=True, spill=156.6) is None)
    check("ec2_high_fetchwait_vetoed", promote(is_ec2=True, fetch_wait=47.6) is None)

    # 18. Promoted memory is always the 219G tier (only 32c tier that
    #     preserves 6.75 GB/core) — encoded at the call site; here we
    #     assert the tier exists and is the max.
    check("promotion_tier_is_219", XLARGE_MEM_TIERS[-1] == 219)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURES: {FAILURES}")
        return 1
    print("All 32c worker tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

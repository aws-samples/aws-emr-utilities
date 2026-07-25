#!/usr/bin/env python3
"""Unit tests for Rule 3: over-provisioned Serverless source sizing.

Covers _compute_exec_limits directly with synthetic cases derived from real
runs (field report: 454-exec uncapped suite; BDA validation runs 2026-07).

Usage: python3 tests/test_exec_limits_rule3.py
Exit codes: 0 = pass, 1 = failure.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from emr_s_fine_tuner import _compute_exec_limits

FAILURES = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name} {detail}")
    if not cond:
        FAILURES.append(name)


def limits(orig_executors, cores_per_exec, duration_hours, task_exec_hours,
           idle_pct, mode="cost", vcpu=4, input_gb=2100.0):
    return _compute_exec_limits(
        input_gb, vcpu, 0,
        mem_pct=25.0, cpu_pct=50.0, idle_pct=idle_pct, spill_gb=0.0,
        mode=mode,
        orig_executors=orig_executors, orig_cores=cores_per_exec,
        orig_mem_mb=30 * 1024,
        total_task_exec_hours=task_exec_hours, duration_hours=duration_hours,
        stages=[], is_ec2_source=False,
    )


def main():
    print("Rule 3 unit tests — _compute_exec_limits, Serverless sources")

    # 1. Field-report scenario: uncapped suite, 454 execs × 4c, 947s,
    #    eff ≈ 0.20, idle 80%. Old anchor gave 499; work-based ≈ 116.
    dur = 947 / 3600
    task_h = 0.20 * 454 * 4 * dur  # eff 0.20
    mx, mn = limits(454, 4, dur, task_h, idle_pct=80.0)
    check("neil_uncapped_suite_capped_down", 90 <= mx <= 150,
          f"(max_exec={mx}, was 499 pre-fix)")
    check("neil_min_exec_third", mn == max(3, mx // 3), f"(min={mn})")

    # 2. Healthy Serverless source: anchor preserved exactly (orig×1.1).
    dur = 1.0
    task_h = 0.88 * 43 * 4 * dur  # eff 0.88, busy cluster
    mx, _ = limits(43, 4, dur, task_h, idle_pct=9.0)
    expected = max(4, int(43 * 4 * 1.1 / 4))
    check("healthy_source_anchor_preserved", mx == expected,
          f"(max_exec={mx}, expected {expected})")

    # 3. Capped-100 suite source (eff 0.58, idle 30) — Rule 3 must NOT fire.
    dur = 1565 / 3600
    task_h = 0.58 * 110 * 4 * dur
    mx, _ = limits(110, 4, dur, task_h, idle_pct=30.0)
    expected = max(4, int(110 * 4 * 1.1 / 4))
    check("capped100_suite_unchanged", mx == expected, f"(max_exec={mx})")

    # 4. Short-spike single query (q96-like): low eff but LOW idle —
    #    executors were busy, run just short. Rule 3 must NOT fire.
    dur = 175 / 3600
    task_h = 0.18 * 72 * 4 * dur
    mx, _ = limits(72, 4, dur, task_h, idle_pct=30.0)
    expected = max(4, int(72 * 4 * 1.1 / 4))
    check("short_spike_unchanged", mx == expected, f"(max_exec={mx})")

    # 5. Small job below the scale guard (orig_vcpu < 200): unchanged even
    #    when idle and inefficient.
    dur = 0.5
    task_h = 0.15 * 40 * 4 * dur
    mx, _ = limits(40, 4, dur, task_h, idle_pct=75.0)
    expected = max(4, int(40 * 4 * 1.1 / 4))
    check("small_job_unchanged", mx == expected, f"(max_exec={mx})")

    # 6. Rule 3 + performance mode: ×1.5 applies on the REDUCED base.
    dur = 947 / 3600
    task_h = 0.20 * 454 * 4 * dur
    mx_cost, _ = limits(454, 4, dur, task_h, idle_pct=80.0, mode="cost")
    mx_perf, _ = limits(454, 4, dur, task_h, idle_pct=80.0, mode="performance")
    check("perf_mode_scales_reduced_base",
          mx_perf == int(mx_cost * 1.5) and mx_perf < 250,
          f"(cost={mx_cost}, perf={mx_perf})")

    # 7. Zero orig_vcpu fallback: work-only sizing unchanged.
    dur = 1.0
    mx, _ = limits(0, 0, dur, 120.0, idle_pct=50.0)
    expected = max(4, int(120.0 * 1.1 / 4))
    check("zero_orig_vcpu_fallback", mx == expected, f"(max_exec={mx})")

    # 8b. EC2 Rule 2 vCPU guard: a fat-executor idle EC2 fleet must not
    #     escape work-based sizing just because its executor COUNT is low.
    #     100 x 32c = 3,200 vCPU at eff 0.20/idle 80% — pre-guard, Rule 2
    #     required >150 executors and copied the waste forward.
    def ec2_limits(orig_executors, cores, eff, dur_h=1.0, input_gb=3000.0):
        task_h = eff * orig_executors * cores * dur_h
        return _compute_exec_limits(
            input_gb, 4, 0, mem_pct=25.0, cpu_pct=50.0, idle_pct=80.0,
            spill_gb=0.0, mode="cost", orig_executors=orig_executors,
            orig_cores=cores, orig_mem_mb=64 * 1024,
            total_task_exec_hours=task_h, duration_hours=dur_h,
            stages=[], is_ec2_source=True)

    fat_idle, _ = ec2_limits(100, 32, eff=0.20)
    # anchor-based sizing would give ~ 3200 * base_eff(0.80) / 4 = 640 execs
    check("ec2_fat_idle_capped", fat_idle < 400, f"(max_exec={fat_idle})")
    thin_ok, _ = ec2_limits(100, 8, eff=0.20)   # 800 vCPU — below both guards
    anchor = int(100 * 8 * (0.47 + 0.33 * min(1.0, (64/8) / 6.75)) / 4)
    check("ec2_small_fleet_unguarded", thin_ok >= anchor - 1,
          f"(max_exec={thin_ok}, anchor~{anchor})")
    fat_busy, _ = ec2_limits(100, 32, eff=0.75)  # efficient — Rule 2 must not fire
    check("ec2_fat_busy_unchanged", fat_busy > 400, f"(max_exec={fat_busy})")

    # 8. Monotonicity: Rule 3 can only reduce vs the old anchor.
    for eff in (0.05, 0.15, 0.25, 0.35, 0.39):
        dur = 0.5
        task_h = eff * 300 * 4 * dur
        mx, _ = limits(300, 4, dur, task_h, idle_pct=70.0)
        old = max(4, int(300 * 4 * 1.1 / 4))
        check(f"monotonic_eff_{eff}", mx <= old, f"(max_exec={mx} <= {old})")

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURES: {FAILURES}")
        return 1
    print("All Rule 3 tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""EMR Test Drive CLI.

    etd validate  --config my.yaml      # check config and account, create nothing
    etd setup     --config my.yaml      # create applications, stage assets, build test bed
    etd run       --config my.yaml      # run workloads on every variant, then report
    etd report    --config my.yaml      # rebuild the report from an existing run
    etd status    --config my.yaml      # what exists in the account for this run
    etd teardown  --config my.yaml      # delete everything this run created
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from .bootstrap import bootstrap, delete_iam
from . import lakeformation
from .compare import build_all_pairs, load_run
from .live import Orchestrator
from .report import render_html, render_json
from .spec import ConfigError, RunSpec, load_spec

GATED_INTENTS = {"upgrade_regression", "patch_validation"}


def _session(spec: RunSpec, profile: str | None):
    try:
        import boto3  # noqa: F401
    except ImportError:
        sys.exit("boto3 is required for live commands:  python3 -m pip install boto3")
    from .aws import SessionFactory
    factory = SessionFactory(profile, spec.region, spec.credential_refresh_command)
    if factory.refresher.enabled:
        print(f"  credential auto-refresh enabled")
    return factory


def _verify_account(session, spec: RunSpec) -> None:
    ident = session.client("sts").get_caller_identity()
    if ident["Account"] != spec.account:
        sys.exit(f"Credentials are for account {ident['Account']} but the config says "
                 f"{spec.account}. Refusing to touch the wrong account.")
    print(f"  account {ident['Account']} as {ident['Arn'].split('/')[-1]}")


def _run_dir(spec: RunSpec, run_id: str | None = None) -> Path:
    base = Path("runs") / spec.name
    if run_id:
        return base / run_id
    if not base.exists():
        return base / "latest"
    runs = sorted([p for p in base.iterdir() if p.is_dir() and (p / "run_manifest.json").exists()])
    return runs[-1] if runs else base / "latest"


def _estimate(spec: RunSpec) -> tuple[int, float]:
    jobs = 0
    for w in spec.workloads:
        jobs += len(w.formats or ["parquet"]) if w.kind == "functional" else 1
    jobs *= len(spec.variants)
    jobs += 1  # test bed
    # Rough: each job burns (driver + executors) vCPU for a few minutes.
    v = spec.variants[0]
    vcpu = v.shape["driver_cores"] + v.shape["executor_cores"] * v.shape["executor_count"]
    hours = jobs * (4 / 60)
    usd = vcpu * hours * 0.052624 + vcpu * 4 * hours * 0.0057785
    return jobs, usd


# --------------------------------------------------------------------- commands

def cmd_validate(args) -> int:
    spec = load_spec(args.config)
    print(f"config OK: run={spec.name} region={spec.region} account={spec.account}")
    print(f"  bucket        s3://{spec.bucket}/{spec.prefix}/{spec.name}")
    print(f"  variants      {len(spec.variants)}")
    for v in spec.variants:
        print(f"    - {v.variant_id:16s} {v.release_label:16s} {v.architecture:7s} "
              f"{v.access_mode:8s} {'(baseline)' if v.baseline else ''}")
    print(f"  workloads     {len(spec.workloads)}")
    for w in spec.workloads:
        detail = ", ".join(w.formats) if w.kind == "functional" else f"{len(w.queries)} queries"
        print(f"    - {w.workload_id:16s} {w.kind:12s} iterations={w.iterations}  {detail}")
    print(f"  comparisons   {len(spec.comparisons)}")
    for c in spec.comparisons:
        print(f"    - {c['comparison_id']:16s} {c['baseline']} -> {c['candidate']} ({c['intent']})")
    jobs, usd = _estimate(spec)
    print(f"  estimate      ~{jobs} jobs, ~${usd:.2f}")
    if not args.no_aws:
        session = _session(spec, args.profile)
        _verify_account(session, spec)
        s3 = session.client("s3")
        try:
            s3.head_bucket(Bucket=spec.bucket)
            print(f"  bucket        reachable")
        except Exception as exc:  # noqa: BLE001
            print(f"  bucket        NOT reachable: {exc}")
            return 1
        iam = session.client("iam")
        try:
            iam.get_role(RoleName=spec.execution_role_arn.split("/")[-1])
            print(f"  exec role     exists")
        except Exception as exc:  # noqa: BLE001
            print(f"  exec role     NOT found: {exc}")
            return 1
    return 0


def cmd_bootstrap(args) -> int:
    """Create the bucket and execution role a new account needs."""
    spec = load_spec(args.config)
    session = _session(spec, args.profile)
    _verify_account(session, spec)
    bootstrap(session, spec, create_bucket=not args.no_bucket)
    return 0


def cmd_setup(args) -> int:
    spec = load_spec(args.config)
    session = _session(spec, args.profile)
    _verify_account(session, spec)
    jobs, usd = _estimate(spec)
    if spec.safety.get("confirm_before_provision") and not args.yes:
        print(f"\nAbout to create {len(spec.variants)} EMR Serverless application(s) and run "
              f"~{jobs} jobs (~${usd:.2f}, auto-stop after "
              f"{spec.safety['auto_stop_minutes']} idle minutes).")
        if input("Proceed? [y/N] ").strip().lower() not in ("y", "yes"):
            print("aborted")
            return 130
    orch = Orchestrator(session, spec)
    state = orch.setup()
    d = _run_dir(spec, orch.run_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / "setup.json").write_text(json.dumps(
        {"run_id": orch.run_id, "asset_uri": state["asset_uri"],
         "applications": {v.variant_id: v.application_id for v in spec.variants},
         "job_log": orch.job_log}, indent=2, default=str) + "\n")
    print(f"\nsetup complete — run_id {orch.run_id}\n  state: {d / 'setup.json'}")
    return 0


def cmd_run(args) -> int:
    spec = load_spec(args.config)
    session = _session(spec, args.profile)
    _verify_account(session, spec)

    prior = None
    for cand in sorted((Path("runs") / spec.name).glob("*/setup.json"), reverse=True) \
            if (Path("runs") / spec.name).exists() else []:
        prior = json.loads(cand.read_text())
        break
    if not prior:
        print("No prior setup found — running setup first.")
        rc = cmd_setup(args)
        if rc:
            return rc
        for cand in sorted((Path("runs") / spec.name).glob("*/setup.json"), reverse=True):
            prior = json.loads(cand.read_text())
            break

    orch = Orchestrator(session, spec, run_id=prior["run_id"])
    for v in spec.variants:
        v.application_id = prior["applications"].get(v.variant_id)
        if not v.application_id:
            orch.provider.provision(v)
    orch.job_log = list(prior.get("job_log") or [])

    results = orch.run(prior["asset_uri"])
    if not results:
        print("\nNo results produced — nothing to report.")
        return 1
    d = _run_dir(spec, orch.run_id)
    orch.write_run_dir(results, d)
    print(f"\nresults written to {d}")
    return _build_report(spec, d, open_it=args.open, fail_on=args.fail_on)


def cmd_report(args) -> int:
    spec = load_spec(args.config)
    d = Path(args.run_dir) if args.run_dir else _run_dir(spec)
    if not (d / "run_manifest.json").exists():
        sys.exit(f"No run manifest at {d}. Run `etd run` first, or pass --run-dir.")
    return _build_report(spec, d, open_it=args.open, fail_on=args.fail_on)


def _reresolve_expectations(run) -> int:
    """Recompute expected_state for functional units from the current matrices.

    Measurements are immutable; *expectations* are metadata. When a support
    matrix is corrected, the stored results should be re-judged rather than
    re-measured.
    """
    from . import support
    variants = run.variants
    n = 0
    for (vid, _wid), payload in run.units.items():
        if payload.get("unit_kind") != "operation":
            continue
        v = variants.get(vid) or {}
        mode, rel = v.get("access_mode", "plain"), v.get("release_label", "")
        for u in payload.get("units", []):
            state, reason = support.expected_state(mode, u.get("table_format", ""),
                                                  u.get("name", ""), rel)
            if state != u.get("expected_state"):
                n += 1
            u["expected_state"], u["expected_reason"] = state, reason
            u["lf_permissions"] = support.lf_permissions(
                mode, u.get("table_format", ""), u.get("name", ""))
    return n


def _build_report(spec: RunSpec, run_dir: Path, open_it: bool, fail_on: str) -> int:
    run = load_run(run_dir)
    if not run.units:
        sys.exit(f"No unit files under {run_dir / 'units'}")
    changed = _reresolve_expectations(run)
    if changed:
        print(f"  re-resolved {changed} expected-state value(s) from the current matrices")
    results = build_all_pairs(run)
    out = run_dir / "out"
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.html").write_text(render_html(run, results))
    (out / "report.json").write_text(render_json(run, results))

    print(f"\n{'':2}{'verdict':16s}{'comparison':46s}{'match':22s}summary")
    for r in results:
        if not r["comparison"].get("declared"):
            continue
        fc = (r["functional"] or {}).get("counts", {})
        pc = (r["performance"] or {}).get("counts", {})
        newc = len([f for f in r["correctness"]
                    if not f["pre_existing"] and not f.get("resolved")])
        print(f"  {r['verdict']['level']:16s}{r['comparison']['title'][:44]:46s}"
              f"{r['match']['status']:22s}"
              f"newfail={fc.get('NEW_FAILURE', 0)} fixed={fc.get('FIXED', 0) + fc.get('FIXED_BY_RELEASE', 0)} "
              f"corr={newc} regr={pc.get('REGRESSION', 0)} overhead={pc.get('OVERHEAD', 0)} "
              f"timeout={pc.get('NEW_TIMEOUT', 0)}")
    print(f"\nhtml  {out / 'report.html'}\njson  {out / 'report.json'}")
    if open_it:
        subprocess.run(["open", str(out / "report.html")], check=False)

    if fail_on:
        cats = {c.strip() for c in fail_on.split(",") if c.strip()}
        problems = []
        for r in results:
            if (r["comparison"]["intent"] not in GATED_INTENTS
                    or not r["comparison"].get("declared")):
                continue
            cid = r["comparison"]["comparison_id"]
            fc = (r["functional"] or {}).get("counts", {})
            if "new_failure" in cats and fc.get("NEW_FAILURE"):
                problems.append(f"{cid}: {fc['NEW_FAILURE']} new functional failure(s)")
            if "correctness" in cats:
                new = [f for f in r["correctness"]
                       if not f["pre_existing"] and not f.get("resolved")]
                if new:
                    problems.append(f"{cid}: {len(new)} new correctness finding(s)")
            perf = r["performance"]
            if perf and r["match"]["perf_verdict_valid"]:
                if "regression" in cats and perf["counts"].get("REGRESSION"):
                    problems.append(f"{cid}: {perf['counts']['REGRESSION']} query regression(s)")
                if "timeout" in cats and perf["counts"].get("NEW_TIMEOUT"):
                    problems.append(f"{cid}: {perf['counts']['NEW_TIMEOUT']} new timeout(s)")
        if problems:
            print(f"\nFAIL --fail-on={','.join(sorted(cats))}", file=sys.stderr)
            for p in problems:
                print(f"  - {p}", file=sys.stderr)
            return 1
        print(f"\nPASS --fail-on={','.join(sorted(cats))}")
    return 0


def cmd_status(args) -> int:
    spec = load_spec(args.config)
    session = _session(spec, args.profile)
    _verify_account(session, spec)
    orch = Orchestrator(session, spec)
    apps = []
    for page in orch.provider.client.get_paginator("list_applications").paginate():
        for app in page["applications"]:
            if (app.get("name") or "").startswith(f"etd-{spec.name}-"):
                apps.append(app)
    if not apps:
        print("  no applications for this run")
    for a in apps:
        print(f"  {a['id']}  {str(a.get('name')):44s} {a['state']}")
    d = _run_dir(spec)
    print(f"  local run dir: {d} ({'present' if d.exists() else 'absent'})")
    return 0


def cmd_teardown(args) -> int:
    spec = load_spec(args.config)
    session = _session(spec, args.profile)
    _verify_account(session, spec)
    orch = Orchestrator(session, spec)
    victims = orch.provider.teardown(dry_run=True)
    if not victims:
        print("  nothing tagged for this run — nothing to delete")
    else:
        print("  will delete:")
        for v in victims:
            print(f"    {v}")
        if not args.yes and input("Proceed? [y/N] ").strip().lower() not in ("y", "yes"):
            print("aborted")
            return 130
        orch.provider.teardown()
    if args.delete_data:
        s3 = session.client("s3")
        prefix = f"{spec.prefix}/{spec.name}/"
        print(f"  deleting s3://{spec.bucket}/{prefix} …")
        n = 0
        for page in s3.get_paginator("list_objects_v2").paginate(
                Bucket=spec.bucket, Prefix=prefix):
            objs = [{"Key": o["Key"]} for o in page.get("Contents", [])]
            if objs:
                s3.delete_objects(Bucket=spec.bucket, Delete={"Objects": objs})
                n += len(objs)
        print(f"  deleted {n} objects")
        try:
            session.client("glue").delete_database(Name=spec.database)
            print(f"  deleted Glue database {spec.database}")
        except Exception as exc:  # noqa: BLE001
            print(f"  Glue database not deleted: {exc}")
    if {v.access_mode for v in spec.variants} & {"lf_fta", "lf_fgac"}:
        lakeformation.teardown(session, spec)
    if getattr(args, "delete_iam", False):
        delete_iam(session, spec)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="etd", description="EMR Test Drive")
    ap.add_argument("--config", required=True)
    ap.add_argument("--profile", default=None, help="AWS profile name")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("validate"); p.add_argument("--no-aws", action="store_true"); p.set_defaults(fn=cmd_validate)
    p = sub.add_parser("bootstrap")
    p.add_argument("--no-bucket", action="store_true", help="only create the IAM role")
    p.set_defaults(fn=cmd_bootstrap)
    p = sub.add_parser("setup"); p.add_argument("--yes", action="store_true"); p.set_defaults(fn=cmd_setup)
    p = sub.add_parser("run")
    p.add_argument("--yes", action="store_true"); p.add_argument("--open", action="store_true")
    p.add_argument("--fail-on", default=""); p.set_defaults(fn=cmd_run)
    p = sub.add_parser("report")
    p.add_argument("--run-dir", default=None); p.add_argument("--open", action="store_true")
    p.add_argument("--fail-on", default=""); p.set_defaults(fn=cmd_report)
    p = sub.add_parser("status"); p.set_defaults(fn=cmd_status)
    p = sub.add_parser("teardown")
    p.add_argument("--yes", action="store_true"); p.add_argument("--delete-data", action="store_true")
    p.add_argument("--delete-iam", action="store_true",
                   help="also delete the execution role created by `etd bootstrap`")
    p.set_defaults(fn=cmd_teardown)

    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except ConfigError as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

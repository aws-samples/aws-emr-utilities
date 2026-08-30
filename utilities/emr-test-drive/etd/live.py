"""Live orchestration: stage assets, build the test bed, run workloads, collect results.

Concurrency rule that matters for the numbers: **variants run in parallel,
iterations within a variant run serially**. Parallel iterations contaminate
performance measurements (shared S3 throughput, shared catalog and Lake
Formation quotas); serial variants would triple the wall clock for no benefit.
"""

from __future__ import annotations

import concurrent.futures as cf
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from . import lakeformation, support
from .providers.emr_serverless import EmrServerlessProvider, JobResult
from .spec import RunSpec, Variant, Workload

ASSET_JOB = "etd_job.py"

# Full Table Access is enabled per *job*, not on the application: the EMRFS
# credentials resolver is swapped for the Lake Formation one, and Spark is told
# to defer location validation and folder creation until after the catalog entry
# exists, because LF only vends credentials for a table it already knows about.
# FGAC by contrast is an application-level switch. They cannot be combined.
FTA_CONF = {
    "spark.hadoop.fs.s3.credentialsResolverClass":
        "com.amazonaws.glue.accesscontrol.AWSLakeFormationCredentialResolver",
    "spark.hadoop.fs.s3.useDirectoryHeaderAsFolderObject": "true",
    "spark.hadoop.fs.s3.folderObject.autoAction.disabled": "true",
    "spark.sql.catalog.skipLocationValidationOnCreateTable.enabled": "true",
    "spark.sql.catalog.createDirectoryAfterTable.enabled": "true",
    "spark.sql.catalog.dropDirectoryBeforeTable.enabled": "true",
}


def access_mode_conf(v: Variant, spec: RunSpec) -> dict:
    """Job-level configuration implied by the variant's access mode."""
    if v.access_mode == "lf_fta":
        return dict(FTA_CONF)
    if v.access_mode == "lf_fgac":
        # FGAC forbids pinning the executor count:
        #   ValidationException: Spark Dynamic Resource Allocation feature cannot
        #   be disabled when Lake Formation is enabled.
        # The job runs two resource profiles (user and system) and needs dynamic
        # allocation to place executors for both. maxExecutors is shared between
        # the profiles -- EMR Serverless gives each up to 90% of it by default --
        # so it is set to the configured count and left unpinned at the bottom,
        # because pinning min == max can deadlock one profile out of capacity.
        #
        # Consequence for measurement: an FGAC variant cannot be executor-matched
        # to a plain one, which is why an FGAC comparison carries a sizing caveat
        # and is reported as governance overhead rather than as a benchmark.
        want = int(v.shape.get("executor_count", 2))
        return {
            "spark.dynamicAllocation.enabled": "true",
            "spark.dynamicAllocation.maxExecutors": str(max(2, want)),
            "spark.dynamicAllocation.initialExecutors": str(max(1, want // 2)),
            "spark.dynamicAllocation.minExecutors": "1",
        }
    return {}


# Per-format Spark configuration. The 7.10 boundary matters: before it, Delta and
# Hudi needed the record-server SQL extension and lf.managed on the catalog, and
# Iceberg used SparkCatalog rather than SparkSessionCatalog. Getting this wrong
# is itself a classic upgrade failure, so it is encoded rather than assumed.
def format_conf(fmt: str, spec: RunSpec, v: Variant) -> dict:
    warehouse = f"{spec.data_uri}/{spec.database}/"
    if fmt == "parquet":
        return {}
    if fmt == "iceberg":
        # catalog-impl=GlueCatalog is required: with type=hive Iceberg tries to
        # open a thrift connection to a Hive metastore that does not exist on
        # EMR Serverless and fails with RuntimeMetaException.
        conf = {
            "spark.sql.extensions": "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions",
            "spark.sql.catalog.spark_catalog": "org.apache.iceberg.spark.SparkSessionCatalog",
            "spark.sql.catalog.spark_catalog.catalog-impl": "org.apache.iceberg.aws.glue.GlueCatalog",
            "spark.sql.catalog.spark_catalog.warehouse": warehouse,
            "spark.sql.catalog.spark_catalog.io-impl": "org.apache.iceberg.aws.s3.S3FileIO",
            "spark.sql.catalog.spark_catalog.client.region": spec.region,
        }
        if v.access_mode == "lf_fta":
            # Iceberg vends its own credentials through the Glue catalog, so it
            # needs the Lake Formation flag rather than the EMRFS resolver.
            # `type` and `catalog-impl` are mutually exclusive in Iceberg, so the
            # catalog-impl set above must be removed when switching to type=glue.
            conf.pop("spark.sql.catalog.spark_catalog.catalog-impl", None)
            conf["spark.sql.catalog.spark_catalog.type"] = "glue"
            conf["spark.sql.catalog.spark_catalog.glue.account-id"] = spec.account
            conf["spark.sql.catalog.spark_catalog.glue.lakeformation-enabled"] = "true"
            conf["spark.sql.catalog.dropDirectoryBeforeTable.enabled"] = "true"
        elif v.access_mode == "lf_fgac":
            # Under FGAC do NOT set type=glue. The record server configures the
            # catalog for the system-profile driver, and adding `type` on top of
            # `catalog-impl` makes metadata operations fail with
            #   IllegalArgumentException: Cannot create catalog spark_catalog,
            #   both type and catalog-impl are set
            # while data operations still succeed -- observed on 7.13 for
            # DESCRIBE and SHOW CREATE TABLE only.
            conf["spark.sql.catalog.spark_catalog.glue.account-id"] = spec.account
            conf["spark.sql.catalog.spark_catalog.glue.lakeformation-enabled"] = "true"
            conf["spark.sql.catalog.dropDirectoryBeforeTable.enabled"] = "true"
        if v.access_mode == "lf_fgac" and not support._ge(v.release_label, "7.10.0"):
            conf["spark.sql.catalog.spark_catalog"] = "org.apache.iceberg.spark.SparkCatalog"
            conf["spark.sql.catalog.spark_catalog.lf.managed"] = "true"
        return conf
    if fmt == "delta":
        conf = {
            "spark.sql.extensions": "io.delta.sql.DeltaSparkSessionExtension",
            "spark.sql.catalog.spark_catalog": "org.apache.spark.sql.delta.catalog.DeltaCatalog",
        }
        if v.access_mode == "lf_fgac" and not support._ge(v.release_label, "7.10.0"):
            conf["spark.sql.extensions"] = (
                "io.delta.sql.DeltaSparkSessionExtension,"
                "com.amazonaws.emr.recordserver.connector.spark.sql.RecordServerSQLExtension")
            conf["spark.sql.catalog.spark_catalog.lf.managed"] = "true"
        return conf
    raise ValueError(f"Unsupported format: {fmt}")


class Orchestrator:
    def __init__(self, factory, spec: RunSpec, run_id: str | None = None) -> None:
        self.spec = spec
        self.factory = factory
        self.s3 = factory.client("s3")
        self.provider = EmrServerlessProvider(factory, spec)
        self.run_id = run_id or f"etd-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
        self.job_log: list[dict] = []

    # ------------------------------------------------------------------ assets

    def stage_assets(self) -> str:
        local = Path(__file__).parent / "assets" / ASSET_JOB
        key = f"{self.spec.prefix}/{self.spec.name}/assets/{ASSET_JOB}"
        self.s3.put_object(Bucket=self.spec.bucket, Key=key, Body=local.read_bytes())
        uri = f"s3://{self.spec.bucket}/{key}"
        print(f"  staged {uri}")
        return uri

    def _put(self, uri: str, payload: dict) -> None:
        bucket, key = uri.replace("s3://", "").split("/", 1)
        self.s3.put_object(Bucket=bucket, Key=key,
                           Body=(json.dumps(payload, indent=2, default=str) + "\n").encode(),
                           ContentType="application/json")

    def _get(self, uri: str) -> dict | None:
        bucket, key = uri.replace("s3://", "").split("/", 1)
        try:
            return json.loads(self.s3.get_object(Bucket=bucket, Key=key)["Body"].read())
        except self.s3.exceptions.NoSuchKey:
            return None
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------- job helpers

    def _run_job(self, v: Variant, asset_uri: str, mode: str, job_cfg: dict,
                 out_uri: str, extra_conf: dict, label: str) -> tuple[JobResult, dict | None]:
        name = f"etd-{self.spec.name}-{v.variant_id}-{label}"[:255]
        job_id = self.provider.submit(
            v, name, asset_uri,
            ["--mode", mode, "--config", json.dumps(job_cfg), "--output", out_uri],
            extra_conf)
        print(f"  [{v.variant_id}] {label}: submitted {job_id}")
        t0 = time.time()
        res = self.provider.monitor(
            v, job_id,
            on_state=lambda s: print(f"  [{v.variant_id}] {label}: {s} "
                                     f"(+{int(time.time()-t0)}s)"))
        doc = self._get(out_uri)
        self.job_log.append({
            "variant_id": v.variant_id, "label": label, "mode": mode,
            "job_run_id": job_id, "state": res.state, "wall_clock_s": res.wall_clock_s,
            "billed": res.billed, "state_details": res.state_details[:500],
            "result_doc": out_uri, "result_present": doc is not None,
        })
        status = res.state if res.ok else f"{res.state}: {res.state_details[:200]}"
        print(f"  [{v.variant_id}] {label}: {status} in {res.wall_clock_s:.0f}s "
              f"(billed {res.billed.get('vcpu_hour', 0):.3f} vCPU-hr)")
        return res, doc

    # ------------------------------------------------------------------- setup

    def setup(self) -> dict:
        print(f"\n== setup ({len(self.spec.variants)} variant(s)) ==")
        asset_uri = self.stage_assets()
        for v in self.spec.variants:
            self.provider.provision(v)

        tb = self.spec.testbed
        if tb.get("mode", "generate") != "generate":
            print(f"  testbed mode={tb.get('mode')} — using existing database "
                  f"{self.spec.database}, skipping generation")
            return {"asset_uri": asset_uri, "testbed": "existing"}

        # Build the test bed once, on the baseline variant. Every variant reads
        # the same physical data through the same Glue database.
        base = next(v for v in self.spec.variants if v.baseline)
        cfg = {
            "database": self.spec.database, "data_uri": self.spec.data_uri,
            "fact_rows": (tb.get("scale") or {}).get("fact_rows", 2_000_000),
            "dim_rows": (tb.get("scale") or {}).get("dim_rows", 20_000),
            "variant_id": base.variant_id, "workload_id": "testbed",
            "release_label": base.release_label,
        }
        out = f"{self.spec.results_uri}/{self.run_id}/testbed.json"
        res, doc = self._run_job(base, asset_uri, "setup", cfg, out, {}, "testbed")
        if not res.ok:
            raise RuntimeError(f"Test bed setup failed: {res.state_details[:500]}")
        if doc:
            print(f"  testbed rows: {doc.get('row_counts')}")

        lf_state = self.setup_lakeformation()
        return {"asset_uri": asset_uri, "testbed": doc or {}, "lakeformation": lf_state}

    def setup_lakeformation(self) -> dict:
        """Register the test bed with Lake Formation if any variant needs it."""
        modes = {v.access_mode for v in self.spec.variants}
        if not (modes & {"lf_fta", "lf_fgac"}):
            return {}
        caller = self.factory.client("sts").get_caller_identity()["Arn"]
        # An assumed-role ARN is not a valid Lake Formation principal; convert it
        # back to the underlying role ARN.
        if ":assumed-role/" in caller:
            parts = caller.split("/")
            caller = (f"arn:aws:iam::{self.spec.account}:role/{parts[1]}"
                      if len(parts) > 1 else caller)
        return lakeformation.setup(self.factory, self.spec, caller)

    # --------------------------------------------------------------------- run

    @property
    def perf_sink(self) -> str:
        """One sink for every variant in the run.

        `noop` is rejected by the FGAC record server, so if any variant uses
        FGAC the whole run falls back to `count`. Mixing sinks across variants
        would invalidate the comparison.
        """
        if any(v.access_mode == "lf_fgac" for v in self.spec.variants):
            return "count"
        return "noop"

    def run(self, asset_uri: str) -> dict:
        print(f"\n== run {self.run_id} ==")
        # Always re-stage: otherwise a fix to the job script silently does not
        # reach the cluster and you debug last run's code.
        asset_uri = self.stage_assets()
        results: dict[tuple[str, str], dict] = {}
        max_par = int(self.spec.safety["max_parallel_variants"])

        def run_variant(v: Variant) -> list[tuple[tuple[str, str], dict]]:
            out: list[tuple[tuple[str, str], dict]] = []
            for w in self.spec.workloads:
                payload = (self._run_functional(v, w, asset_uri) if w.kind == "functional"
                           else self._run_perf(v, w, asset_uri))
                out.append(((v.variant_id, w.workload_id), payload))
            return out

        with cf.ThreadPoolExecutor(max_workers=max(1, min(max_par, len(self.spec.variants)))) as ex:
            futures = {ex.submit(run_variant, v): v for v in self.spec.variants}
            for fut in cf.as_completed(futures):
                v = futures[fut]
                try:
                    for key, payload in fut.result():
                        results[key] = payload
                except Exception as exc:  # noqa: BLE001
                    print(f"  [{v.variant_id}] FAILED: {type(exc).__name__}: {exc}")
        return results

    def _run_functional(self, v: Variant, w: Workload, asset_uri: str) -> dict:
        units: list[dict] = []
        billed = {"vcpu_hour": 0.0, "memory_gb_hour": 0.0, "storage_gb_hour": 0.0,
                  "wall_clock_s": 0.0, "source": "emr-serverless billedResourceUtilization"}
        formats = w.formats or ["parquet"]

        for fmt in formats:
            cfg = {
                "database": self.spec.database, "data_uri": self.spec.data_uri, "format": fmt,
                # Data cell filters are only meaningful under FGAC. With plain
                # Glue or full table access every row is legitimately visible, so
                # asserting that rows were filtered there would manufacture a
                # finding rather than detect one.
                "operations": _ops_for(w, v),
                "variant_id": v.variant_id, "workload_id": w.workload_id,
                "release_label": v.release_label,
                # Isolate this variant's scratch tables from every other variant's.
                "table_suffix": v.variant_id.replace("-", "_").lower(),
            }
            out = (f"{self.spec.results_uri}/{self.run_id}/{v.variant_id}/"
                   f"{w.slug()}/{fmt}.json")
            conf = {**access_mode_conf(v, self.spec), **format_conf(fmt, self.spec, v)}
            res, doc = self._run_job(v, asset_uri, "functional", cfg, out,
                                     conf, f"{w.slug()}-{fmt}")
            billed["vcpu_hour"] += res.billed.get("vcpu_hour", 0.0)
            billed["memory_gb_hour"] += res.billed.get("memory_gb_hour", 0.0)
            billed["storage_gb_hour"] += res.billed.get("storage_gb_hour", 0.0)
            billed["wall_clock_s"] += res.wall_clock_s

            if doc and doc.get("units"):
                for u in doc["units"]:
                    units.append(self._enrich_functional_unit(u, v, fmt))
            else:
                # The job itself never landed: record every operation as such so
                # the comparison shows a hole rather than silently omitting it.
                detail = (doc or {}).get("error") or res.state_details or res.state
                for op in (cfg["operations"] or _default_ops()):
                    units.append(self._enrich_functional_unit(
                        {"name": op, "table_format": fmt, "status": "JOB_FAILED",
                         "error": f"job {res.state}: {detail}"[:1000], "duration_s": None}, v, fmt))

        billed["drivers_per_job"] = 2 if v.access_mode == "lf_fgac" else 1
        return {
            "run_id": self.run_id, "variant_id": v.variant_id, "workload_id": w.workload_id,
            "unit_kind": "operation", "iterations": w.iterations,
            "cost_facts": billed, "data_class": "REAL", "units": units,
        }

    def _enrich_functional_unit(self, u: dict, v: Variant, fmt: str) -> dict:
        state, reason = support.expected_state(v.access_mode, fmt, u["name"], v.release_label)
        u = dict(u)
        u["table_format"] = fmt
        u.setdefault("table_type", "managed")
        u["expected_state"] = state
        u["expected_reason"] = reason
        u["lf_permissions"] = support.lf_permissions(v.access_mode, fmt, u["name"])
        # Row-count shortfall on a nominally successful write is the silent
        # data-loss signal; surface it where the compare layer looks.
        exp = u.get("expected_row_count")
        if (u.get("status") == "SUCCESS" and exp is not None
                and u.get("row_count") is not None and u["row_count"] < exp):
            u["table_version_advanced"] = u.get("table_version_advanced", False)
            u["defect_note"] = (f"operation reported success but the table holds "
                                f"{u['row_count']} of {exp} expected rows")
        return u

    def _run_perf(self, v: Variant, w: Workload, asset_uri: str) -> dict:
        """Run the SQL workload as `job_repeats` separate job runs.

        Iterations inside a single job run share the same driver and executors,
        so they measure *query* variance only. A whole-job effect — different
        host, noisy neighbour, cold vs warm capacity — is invisible to them and
        shows up as a large, confident, and entirely fake delta between
        variants. Running the workload as several independent job runs and
        pooling the timings makes that variance visible instead.
        """
        repeats = max(1, int(w.job_repeats))
        pooled: dict[str, dict] = {}
        per_run: dict[str, list[list[float]]] = {}
        billed = {"vcpu_hour": 0.0, "memory_gb_hour": 0.0, "storage_gb_hour": 0.0,
                  "wall_clock_s": 0.0, "source": "emr-serverless billedResourceUtilization"}

        for r in range(repeats):
            cfg = {
                "database": self.spec.database, "queries": w.queries,
                "iterations": w.iterations, "variant_id": v.variant_id,
                "workload_id": w.workload_id, "release_label": v.release_label,
                "warmup": True, "job_repeat": r + 1, "sink": self.perf_sink,
            }
            out = (f"{self.spec.results_uri}/{self.run_id}/{v.variant_id}/"
                   f"{w.slug()}-run{r + 1}.json")
            label = w.slug() if repeats == 1 else f"{w.slug()}-run{r + 1}"
            res, doc = self._run_job(v, asset_uri, "perf", cfg, out,
                                     access_mode_conf(v, self.spec), label)
            billed["vcpu_hour"] += res.billed.get("vcpu_hour", 0.0)
            billed["memory_gb_hour"] += res.billed.get("memory_gb_hour", 0.0)
            billed["storage_gb_hour"] += res.billed.get("storage_gb_hour", 0.0)
            billed["wall_clock_s"] += res.wall_clock_s

            units = (doc or {}).get("units") or [
                {"name": n, "status": "JOB_FAILED", "iterations": [],
                 "error": f"job {res.state}: {res.state_details[:300]}"} for n in w.queries]
            for u in units:
                p = pooled.setdefault(u["name"], {
                    "name": u["name"], "iterations": [], "status": u.get("status", "SUCCESS"),
                    "error": u.get("error"), "row_count": u.get("row_count"),
                    "job_runs": 0})
                p["iterations"].extend(u.get("iterations") or [])
                p.setdefault("per_job_iterations", []).append(list(u.get("iterations") or []))
                p["job_runs"] += 1
                if u.get("status") not in ("SUCCESS", None) and not p["iterations"]:
                    p["status"] = u["status"]
                    p["error"] = u.get("error")
                per_run.setdefault(u["name"], []).append(list(u.get("iterations") or []))

        for name, p in pooled.items():
            runs = [rr for rr in per_run.get(name, []) if rr]
            if p["iterations"]:
                p["status"] = "SUCCESS"
            if len(runs) > 1:
                bests = [min(rr) for rr in runs]
                p["per_job_best_s"] = bests
                # Spread of the best time across independent job runs. If this is
                # large, no cross-variant delta smaller than it means anything.
                p["between_job_spread_pct"] = round(
                    (max(bests) - min(bests)) / min(bests) * 100.0, 1) if min(bests) else None

        billed["drivers_per_job"] = 2 if v.access_mode == "lf_fgac" else 1
        return {
            "run_id": self.run_id, "variant_id": v.variant_id, "workload_id": w.workload_id,
            "unit_kind": "query", "iterations": w.iterations, "job_repeats": repeats,
            "cost_facts": billed, "data_class": "REAL", "units": list(pooled.values()),
        }

    # ------------------------------------------------------------------ output

    def write_run_dir(self, results: dict, local_dir: Path) -> Path:
        """Write the manifest + unit files that compare/report consume."""
        units_dir = local_dir / "units"
        units_dir.mkdir(parents=True, exist_ok=True)
        for (vid, wid), payload in results.items():
            (units_dir / f"{vid}__{wid.replace('/', '-')}.json").write_text(
                json.dumps(payload, indent=2, default=str) + "\n")

        manifest = {
            "run_id": self.run_id,
            "created_by": "etd",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "region": self.spec.region,
            "account": self.spec.account,
            "data_class": "REAL",
            "data_class_note": (
                "Measured on live EMR Serverless applications in this account. "
                "Expected-support matrices are transcribed from AWS documentation."),
            "scenario": (
                f"{self.spec.name}: "
                + " vs ".join(f"{v.label} [{v.release_label}, {v.access_mode}]"
                              for v in self.spec.variants)
                + f". Test bed: Glue database {self.spec.database}."),
            "variants": [v.to_manifest() for v in self.spec.variants],
            "workloads": [{
                "workload_id": w.workload_id, "kind": w.kind, "unit_kind": w.unit_kind,
                "iterations": w.iterations,
                "description": (f"Operation matrix across {', '.join(w.formats)}"
                                if w.kind == "functional"
                                else (f"{len(w.queries)} SQL queries, best-of-{w.iterations} "
                         f"x {w.job_repeats} job run(s), sink={self.perf_sink}")),
                "dataset": self.spec.data_uri,
                **({"expected_support_matrix": "etd/matrices/<access_mode>.json"}
                   if w.kind == "functional" else
                   {"method": (f"sink={self.perf_sink}; min across iterations and job runs"
                              + ("; noop is unavailable under FGAC so count is used for every "
                                 "variant to keep the comparison valid"
                                 if self.perf_sink == "count" else ""))}),
            } for w in self.spec.workloads],
            "comparisons": self.spec.comparisons,
            "pricing": _pricing(self.spec.region),
            "thresholds": self.spec.thresholds,
            "matrix_provenance": [support.matrix_provenance(m) for m in
                                  sorted({v.access_mode for v in self.spec.variants})],
            "job_log": self.job_log,
        }
        (local_dir / "run_manifest.json").write_text(
            json.dumps(manifest, indent=2, default=str) + "\n")
        return local_dir


def _default_ops() -> list[str]:
    from .assets.etd_job import DEFAULT_OPERATIONS  # local import: asset is standalone
    return list(DEFAULT_OPERATIONS)


# Filter enforcement can only be asserted where a filter exists.
FILTER_OPS = ["ROW_FILTER", "COLUMN_FILTER", "CELL_FILTER"]


def _ops_for(w, v) -> list[str] | None:
    """Operations for this workload on this variant.

    Returns None to mean "the harness default". The filter operations are
    appended only for FGAC, and only when the workload did not name an explicit
    list -- an explicit list is the operator's choice and is left alone.
    """
    if w.operations != "DEFAULT":
        return list(w.operations)
    if v.access_mode != "lf_fgac":
        return None
    return _default_ops() + FILTER_OPS


def _pricing(region: str) -> dict:
    # us-east-1 on-demand EMR Serverless rates. Verify before quoting; the report
    # prints this provenance line verbatim.
    return {
        "as_of": "2026-08-13",
        "source": "pinned fallback table — verify against the AWS Pricing API before quoting",
        "emr_serverless_x86_64": {"vcpu_hour_usd": 0.052624,
                                  "memory_gb_hour_usd": 0.0057785,
                                  "storage_gb_hour_usd": 0.000111},
        "note": ("EMR Serverless with Lake Formation incurs additional charges beyond the "
                 "vCPU/memory rates; not modelled here."),
        "region": region,
    }

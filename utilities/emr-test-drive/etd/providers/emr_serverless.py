"""EMR Serverless provider — application lifecycle, job submission, cost capture.

The one place that talks to the emr-serverless API. Adding EMR on EC2 or EKS
means writing a sibling module with the same five methods; nothing else in the
harness changes.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

from ..spec import RunSpec, Variant

TERMINAL = ("SUCCESS", "FAILED", "CANCELLED", "CANCELLING")
APP_READY = ("CREATED", "STARTED")


@dataclass
class JobResult:
    job_run_id: str
    state: str
    state_details: str
    wall_clock_s: float
    billed: dict
    started_at: Any = None
    ended_at: Any = None

    @property
    def ok(self) -> bool:
        return self.state == "SUCCESS"


def _log(msg: str) -> None:
    print(f"  {msg}", flush=True)


class EmrServerlessProvider:
    deployment_model = "emr_serverless"

    def __init__(self, factory, spec: RunSpec) -> None:
        self.spec = spec
        self.client = factory.client("emr-serverless")

    # ------------------------------------------------------------ lifecycle

    def find_application(self, name: str) -> dict | None:
        paginator = self.client.get_paginator("list_applications")
        for page in paginator.paginate():
            for app in page["applications"]:
                # Some applications come back without a name; skip rather than crash.
                if app.get("name") == name and app.get("state") not in ("TERMINATED",):
                    return app
        return None

    def provision(self, v: Variant) -> str:
        """Create (or reuse) the application for this variant. Returns applicationId."""
        name = self.spec.app_name(v)
        existing = self.find_application(name)
        if existing:
            detail = self.client.get_application(applicationId=existing["id"])["application"]
            if (detail["releaseLabel"] == v.release_label
                    and detail["architecture"] == v.architecture):
                v.application_id = existing["id"]
                _log(f"{v.variant_id}: reusing application {existing['id']} ({name})")
                return existing["id"]
            raise RuntimeError(
                f"Application {name} exists with releaseLabel={detail['releaseLabel']} "
                f"architecture={detail['architecture']} but the config asks for "
                f"{v.release_label}/{v.architecture}. Run `etd teardown` or change run.name.")

        kwargs: dict[str, Any] = {
            "name": name,
            "type": "SPARK",
            "releaseLabel": v.release_label,
            "architecture": v.architecture,
            "tags": self.spec.resource_tags(v),
            "autoStartConfiguration": {"enabled": True},
            "autoStopConfiguration": {
                "enabled": True,
                "idleTimeoutMinutes": int(self.spec.safety["auto_stop_minutes"]),
            },
        }

        runtime_props: dict[str, str] = {}
        if v.access_mode == "lf_fgac":
            # Fine-grained access control is an application-level switch: it puts
            # the record server in the path and gives the job a user-profile and a
            # system-profile driver. It cannot coexist with full-table access.
            runtime_props["spark.emr-serverless.lakeformation.enabled"] = "true"
        if runtime_props:
            kwargs["runtimeConfiguration"] = [
                {"classification": "spark-defaults", "properties": runtime_props}]
        if v.image_uri:
            kwargs["imageConfiguration"] = {"imageUri": v.image_uri}

        resp = self.client.create_application(**kwargs)
        app_id = resp["applicationId"]
        v.application_id = app_id
        _log(f"{v.variant_id}: created application {app_id} ({name}, {v.release_label}, "
             f"{v.architecture}, access_mode={v.access_mode})")
        self.wait_ready(app_id)
        return app_id

    def wait_ready(self, app_id: str, timeout_s: int = 600) -> None:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            state = self.client.get_application(applicationId=app_id)["application"]["state"]
            if state in APP_READY:
                return
            if state in ("TERMINATED",):
                raise RuntimeError(f"Application {app_id} is {state}")
            time.sleep(5)
        raise TimeoutError(f"Application {app_id} not ready within {timeout_s}s")

    # -------------------------------------------------------------- submission

    def spark_submit_parameters(self, v: Variant, extra: dict | None = None) -> str:
        sh = v.shape
        conf = {
            "spark.driver.cores": sh["driver_cores"],
            "spark.driver.memory": sh["driver_memory"],
            "spark.executor.cores": sh["executor_cores"],
            "spark.executor.memory": sh["executor_memory"],
            "spark.executor.instances": sh["executor_count"],
            "spark.dynamicAllocation.enabled": "false",
            "spark.hadoop.hive.metastore.client.factory.class":
                "com.amazonaws.glue.catalog.metastore.AWSGlueDataCatalogHiveClientFactory",
        }
        conf.update(extra or {})
        conf.update(v.spark_conf)          # variant overrides win
        return " ".join(f"--conf {k}={val}" for k, val in conf.items())

    def submit(self, v: Variant, name: str, entry_point: str,
               args: list[str], extra_conf: dict | None = None) -> str:
        resp = self.client.start_job_run(
            applicationId=v.application_id,
            executionRoleArn=self.spec.execution_role_arn,
            name=name[:255],
            executionTimeoutMinutes=int(self.spec.safety["job_timeout_minutes"]),
            tags=self.spec.resource_tags(v),
            jobDriver={"sparkSubmit": {
                "entryPoint": entry_point,
                "entryPointArguments": args,
                "sparkSubmitParameters": self.spark_submit_parameters(v, extra_conf),
            }},
            configurationOverrides={"monitoringConfiguration": {
                "s3MonitoringConfiguration": {"logUri": f"{self.spec.logs_uri}/{v.variant_id}/"}}},
        )
        return resp["jobRunId"]

    def _billed(self, v: Variant, job_run_id: str, attempts: int = 6,
                delay_s: float = 5.0) -> dict:
        """Read billedResourceUtilization, retrying briefly.

        EMR Serverless populates the billed figures a few seconds *after* a job
        reaches a terminal state, so reading them at the transition returns
        zeros. Falls back to totalResourceUtilization if billed never lands.
        """
        billed = {}
        total = {}
        for i in range(attempts):
            jr = self.client.get_job_run(applicationId=v.application_id,
                                         jobRunId=job_run_id)["jobRun"]
            billed = jr.get("billedResourceUtilization") or {}
            total = jr.get("totalResourceUtilization") or {}
            if float(billed.get("vCPUHour", 0)) > 0:
                break
            if i < attempts - 1:
                time.sleep(delay_s)
        src = "billedResourceUtilization"
        if float(billed.get("vCPUHour", 0)) <= 0 and float(total.get("vCPUHour", 0)) > 0:
            billed, src = total, "totalResourceUtilization (billed not yet published)"
        return {
            "vcpu_hour": float(billed.get("vCPUHour", 0.0)),
            "memory_gb_hour": float(billed.get("memoryGBHour", 0.0)),
            "storage_gb_hour": float(billed.get("storageGBHour", 0.0)),
            "source": f"emr-serverless {src}",
        }

    def monitor(self, v: Variant, job_run_id: str,
                poll_s: int = 15, on_state: Callable[[str], None] | None = None) -> JobResult:
        start = time.time()
        last = None
        while True:
            jr = self.client.get_job_run(applicationId=v.application_id, jobRunId=job_run_id)["jobRun"]
            state = jr["state"]
            if state != last and on_state:
                on_state(state)
            last = state
            if state in TERMINAL:
                return JobResult(
                    job_run_id=job_run_id, state=state,
                    state_details=jr.get("stateDetails", ""),
                    wall_clock_s=float(jr.get("totalExecutionDurationSeconds")
                                       or (time.time() - start)),
                    billed=self._billed(v, job_run_id),
                    started_at=jr.get("createdAt"), ended_at=jr.get("updatedAt"),
                )
            time.sleep(poll_s)

    # ---------------------------------------------------------------- teardown

    def teardown(self, dry_run: bool = False) -> list[str]:
        """Delete every application tagged for this run. Tag-scoped by design:
        the harness never deletes anything it did not create."""
        removed = []
        paginator = self.client.get_paginator("list_applications")
        for page in paginator.paginate():
            for app in page["applications"]:
                if not (app.get("name") or "").startswith(f"etd-{self.spec.name}-"):
                    continue
                detail = self.client.get_application(applicationId=app["id"])["application"]
                tags = detail.get("tags", {})
                if tags.get("etd:managed") != "true" or tags.get("etd:run") != self.spec.name:
                    _log(f"skip {app['id']} ({app.get('name')}) — not tagged for this run")
                    continue
                if dry_run:
                    removed.append(f"{app['id']} ({app.get('name')})")
                    continue
                if detail["state"] in ("STARTED", "STARTING"):
                    try:
                        self.client.stop_application(applicationId=app["id"])
                        for _ in range(40):
                            st = self.client.get_application(
                                applicationId=app["id"])["application"]["state"]
                            if st in ("STOPPED", "CREATED"):
                                break
                            time.sleep(5)
                    except Exception as exc:      # noqa: BLE001
                        _log(f"stop {app['id']} failed (continuing): {exc}")
                self.client.delete_application(applicationId=app["id"])
                removed.append(f"{app['id']} ({app.get('name')})")
                _log(f"deleted application {app['id']} ({app.get('name')})")
        return removed

"""EMR Test Drive — configuration and run specification.

Loads the customer-facing YAML (see config.template.yaml), validates it, and
derives everything the rest of the harness needs: variant identities, S3
locations, resource names and tags.

YAML is parsed with PyYAML when available and with a small built-in subset
parser otherwise, so `etd` works on a bare Python install.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "0.2"
VALID_ACCESS_MODES = ("plain", "lf_fta", "lf_fgac")
VALID_INTENTS = ("upgrade_regression", "patch_validation", "governance_overhead")


class ConfigError(Exception):
    pass


# ---------------------------------------------------------------- YAML loading

def _load_yaml(path: Path) -> dict:
    text = path.read_text()
    try:
        import yaml  # type: ignore
        return yaml.safe_load(text)
    except ImportError:
        pass
    try:
        from ruamel.yaml import YAML  # type: ignore
        return YAML(typ="safe").load(text)
    except ImportError as exc:  # pragma: no cover
        raise ConfigError(
            "No YAML parser available. Install one with:  python3 -m pip install pyyaml\n"
            "(or convert your config to JSON and pass it with --config my.json)"
        ) from exc


def load_raw(path: str | Path) -> dict:
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"Config not found: {p}")
    if p.suffix in (".json",):
        return json.loads(p.read_text())
    return _load_yaml(p)


# --------------------------------------------------------------------- model

def _hash(obj: Any, prefix: str) -> str:
    blob = json.dumps(obj, sort_keys=True, default=str)
    return f"{prefix}:{hashlib.sha256(blob.encode()).hexdigest()[:8]}"


@dataclass
class Variant:
    variant_id: str
    label: str
    release_label: str
    architecture: str = "X86_64"
    access_mode: str = "plain"
    baseline: bool = False
    deployment_model: str = "emr_serverless"
    shape: dict = field(default_factory=dict)
    spark_conf: dict = field(default_factory=dict)
    image_uri: str | None = None
    notes: str = ""
    # populated at provision time
    application_id: str | None = None

    @property
    def shape_hash(self) -> str:
        return _hash(self.shape, "sh")

    @property
    def config_hash(self) -> str:
        return _hash({"conf": self.spark_conf, "rel": self.release_label}, "cf")

    @property
    def patch_hash(self) -> str | None:
        return _hash({"image": self.image_uri}, "pt") if self.image_uri else None

    def to_manifest(self) -> dict:
        d = {
            "variant_id": self.variant_id,
            "label": self.label,
            "baseline": self.baseline,
            "deployment_model": self.deployment_model,
            "release_label": self.release_label,
            "architecture": self.architecture.lower().replace("x86_64", "x86_64"),
            "access_mode": self.access_mode,
            "shape": dict(self.shape),
            "shape_hash": self.shape_hash,
            "config_hash": self.config_hash,
            "patch_hash": self.patch_hash,
            "env_handle": {"application_id": self.application_id, "created_by_etd": True},
            "notes": self.notes,
        }
        if self.image_uri:
            d["patch"] = {"id": "image", "description": f"custom image {self.image_uri}",
                          "image": {"uri": self.image_uri}}
        # FGAC runs a user-profile and a system-profile driver.
        d["shape"]["drivers_per_job"] = 2 if self.access_mode == "lf_fgac" else 1
        return d


@dataclass
class Workload:
    workload_id: str
    kind: str
    iterations: int = 1
    job_repeats: int = 1
    formats: list[str] = field(default_factory=list)
    operations: Any = "DEFAULT"
    queries: dict = field(default_factory=dict)

    @property
    def unit_kind(self) -> str:
        return "operation" if self.kind == "functional" else "query"

    def slug(self) -> str:
        return self.workload_id.replace("/", "-")


@dataclass
class RunSpec:
    name: str
    region: str
    account: str
    bucket: str
    prefix: str
    execution_role_arn: str
    credential_refresh_command: str
    tags: dict
    variants: list[Variant]
    workloads: list[Workload]
    comparisons: list[dict]
    testbed: dict
    thresholds: dict
    safety: dict
    raw: dict

    # ---- derived S3 layout
    @property
    def base(self) -> str:
        return f"s3://{self.bucket}/{self.prefix}/{self.name}"

    @property
    def assets_uri(self) -> str:
        return f"{self.base}/assets"

    @property
    def data_uri(self) -> str:
        return f"{self.base}/data"

    @property
    def results_uri(self) -> str:
        return f"{self.base}/results"

    @property
    def logs_uri(self) -> str:
        return f"{self.base}/logs"

    @property
    def out_uri(self) -> str:
        return f"{self.base}/out"

    def app_name(self, v: Variant) -> str:
        return f"etd-{self.name}-{v.variant_id}"[:63]

    @property
    def database(self) -> str:
        return self.testbed.get("database", f"etd_{self.name}".replace("-", "_"))

    def resource_tags(self, v: Variant | None = None) -> dict:
        t = {"etd:run": self.name, "etd:managed": "true", **{k: str(x) for k, x in self.tags.items()}}
        if v:
            t["etd:variant"] = v.variant_id
        return t

    def variant(self, vid: str) -> Variant:
        for v in self.variants:
            if v.variant_id == vid:
                return v
        raise ConfigError(f"Unknown variant id: {vid}")


# ------------------------------------------------------------------ validation

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,30}$")
# A variant id is used verbatim as a local path segment and an S3 key segment,
# so it must be a single safe token. Without this, "../../etc" would write run
# artefacts outside the run directory.
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
# A workload id may contain "/" as a grouping separator ("func/core"), which is
# folded to "-" before it is used as a filename. Parent references and
# backslashes are still refused: the fold only handles forward slashes.
_WORKLOAD_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,63}$")


def load_spec(path: str | Path) -> RunSpec:
    raw = load_raw(path)
    if not isinstance(raw, dict) or "run" not in raw:
        raise ConfigError("Config must be a mapping with a top-level 'run:' block")

    run = raw["run"]
    errs: list[str] = []
    for key in ("name", "region", "account", "bucket", "execution_role_arn"):
        if not run.get(key) or str(run[key]).startswith(("111122223333", "my-etd-bucket")):
            errs.append(f"run.{key} is required and must not be a template placeholder")
    name = str(run.get("name", ""))
    if name and not _NAME_RE.match(name):
        errs.append(f"run.name must match {_NAME_RE.pattern} (got {name!r})")
    # teardown --delete-data scopes deletion to "<prefix>/<name>/", so an empty
    # or wildcard-ish prefix would widen that scope.
    prefix = str(run.get("prefix", "etd") or "")
    if not _ID_RE.match(prefix) or ".." in prefix or "*" in prefix:
        errs.append(f"run.prefix must match {_ID_RE.pattern} (got {prefix!r})")

    variants: list[Variant] = []
    for i, v in enumerate(raw.get("variants") or []):
        if not v.get("id"):
            errs.append(f"variants[{i}].id is required")
            continue
        mode = v.get("access_mode", "plain")
        if mode not in VALID_ACCESS_MODES:
            errs.append(f"variants[{v['id']}].access_mode must be one of {VALID_ACCESS_MODES}")
        if not v.get("release_label"):
            errs.append(f"variants[{v['id']}].release_label is required")
        variants.append(Variant(
            variant_id=v["id"], label=v.get("label", v["id"]),
            release_label=v.get("release_label", ""),
            architecture=str(v.get("architecture", "X86_64")).upper(),
            access_mode=mode, baseline=bool(v.get("baseline")),
            shape=dict(v.get("shape") or {}),
            spark_conf={str(k): str(x) for k, x in (v.get("spark_conf") or {}).items()},
            image_uri=v.get("image_uri"), notes=v.get("notes", ""),
        ))
    if not variants:
        errs.append("at least one variant is required")
    for v in variants:
        if not _ID_RE.match(v.variant_id) or ".." in v.variant_id:
            errs.append(f"variant id must match {_ID_RE.pattern} and contain no '..' (got {v.variant_id!r})")
    if variants and not any(v.baseline for v in variants):
        variants[0].baseline = True

    workloads: list[Workload] = []
    for i, w in enumerate(raw.get("workloads") or []):
        if not w.get("id") or w.get("kind") not in ("functional", "performance"):
            errs.append(f"workloads[{i}] needs an id and kind of functional|performance")
            continue
        wid = str(w["id"])
        if not _WORKLOAD_ID_RE.match(wid) or ".." in wid or wid.startswith("/"):
            errs.append(f"workloads[{i}].id must match {_WORKLOAD_ID_RE.pattern}, "
                        f"contain no '..' and not start with '/' (got {wid!r})")
            continue
        workloads.append(Workload(
            workload_id=w["id"], kind=w["kind"],
            iterations=int(w.get("iterations", 1)),
            job_repeats=int(w.get("job_repeats", 1)),
            formats=list(w.get("formats") or []),
            operations=w.get("operations", "DEFAULT"),
            queries=dict(w.get("queries") or {}),
        ))
    if not workloads:
        errs.append("at least one workload is required")

    ids = {v.variant_id for v in variants}
    comparisons = []
    for i, c in enumerate(raw.get("comparisons") or []):
        for side in ("baseline", "candidate"):
            if c.get(side) not in ids:
                errs.append(f"comparisons[{i}].{side} must be one of {sorted(ids)}")
        if c.get("intent") not in VALID_INTENTS:
            errs.append(f"comparisons[{i}].intent must be one of {VALID_INTENTS}")
        comparisons.append({
            "comparison_id": c.get("id", f"cmp{i}"), "title": c.get("title", c.get("id", "")),
            "baseline": c.get("baseline"), "candidate": c.get("candidate"),
            "intent": c.get("intent"), "primary": bool(c.get("primary")),
            **({"sizing_caveat": c["sizing_caveat"]} if c.get("sizing_caveat") else {}),
        })
    if not comparisons and len(variants) >= 2:
        base = next(v for v in variants if v.baseline)
        for v in variants:
            if v is base:
                continue
            comparisons.append({
                "comparison_id": f"auto-{v.variant_id}",
                "title": f"{base.label} -> {v.label}",
                "baseline": base.variant_id, "candidate": v.variant_id,
                "intent": "patch_validation" if v.image_uri else "upgrade_regression",
                "primary": True,
            })

    if errs:
        raise ConfigError("Invalid config:\n  - " + "\n  - ".join(errs))

    default_shape = {"driver_cores": 2, "driver_memory": "8g", "executor_cores": 2,
                     "executor_memory": "8g", "executor_count": 2}
    for v in variants:
        v.shape = {**default_shape, **v.shape}

    return RunSpec(
        name=name, region=run["region"], account=str(run["account"]),
        bucket=run["bucket"], prefix=run.get("prefix", "etd"),
        execution_role_arn=run["execution_role_arn"],
        credential_refresh_command=str(run.get("credential_refresh_command") or ""),
        tags=dict(run.get("tags") or {}),
        variants=variants, workloads=workloads, comparisons=comparisons,
        testbed=dict(raw.get("testbed") or {"mode": "generate"}),
        thresholds={"perf_noise_band_pct": 5.0, "perf_regression_alert_pct": 10.0,
                    "min_iterations_for_perf_verdict": 2, **(raw.get("thresholds") or {})},
        safety={"auto_stop_minutes": 15, "job_timeout_minutes": 30,
                "max_parallel_variants": 4, "confirm_before_provision": True,
                **(raw.get("safety") or {})},
        raw=raw,
    )

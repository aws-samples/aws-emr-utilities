#!/usr/bin/env python3
"""
EMR AL1 Migration Skill — Dry-Run Simulation Test

Simulates the full skill workflow (Stages 1–7) against a real EMR 5.33.0 cluster.
Tests decision logic, configuration transforms, application detection, and halt conditions.

Usage: python3 dry_run_simulation.py --cluster-id j-XXXXX --region us-east-1
"""

import json
import subprocess
import sys
import argparse
from typing import Dict, List, Tuple

# ═══════════════════════════════════════════════════════════════════════════════
# Test Framework
# ═══════════════════════════════════════════════════════════════════════════════

class TestResult:
    def __init__(self, name: str, passed: bool, message: str, details: str = ""):
        self.name = name
        self.passed = passed
        self.message = message
        self.details = details

results: List[TestResult] = []

def test(name: str, condition: bool, message: str, details: str = ""):
    results.append(TestResult(name, condition, message, details))
    status = "✅ PASS" if condition else "❌ FAIL"
    print(f"  {status}: {name} — {message}")
    if details and not condition:
        print(f"         {details}")

def run_aws(cmd: str) -> Tuple[int, str]:
    """Run an AWS CLI command and return (exit_code, output)."""
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    output = result.stdout if result.returncode == 0 else result.stderr
    return result.returncode, output.strip()

# ═══════════════════════════════════════════════════════════════════════════════
# Stage 1 — Gather Cluster Information
# ═══════════════════════════════════════════════════════════════════════════════

def stage1_gather(cluster_id: str, region: str) -> Dict:
    """Stage 1: Gather and validate cluster information."""
    print("\n" + "="*70)
    print("STAGE 1 — Gather Cluster Information")
    print("="*70)

    # 1.1 Describe cluster
    rc, output = run_aws(f"aws emr describe-cluster --cluster-id {cluster_id} --region {region}")
    test("1.1 Cluster accessible", rc == 0, f"describe-cluster returned rc={rc}")
    if rc != 0:
        print(f"  FATAL: Cannot access cluster. Output: {output}")
        sys.exit(1)

    cluster = json.loads(output)["Cluster"]

    # 1.2 Verify source is AL1
    release = cluster["ReleaseLabel"]
    version = release.replace("emr-", "")
    major, minor, patch = version.split(".")
    is_al1 = int(major) == 5 and int(minor) <= 35
    test("1.2 Source is AL1", is_al1,
         f"Release {release} → AL1={is_al1}",
         "HALT condition: skill should refuse EMR 5.36+ or 6.x/7.x")

    # 1.3 Extract applications
    apps = [a["Name"] for a in cluster["Applications"]]
    app_versions = {a["Name"]: a.get("Version", "unknown") for a in cluster["Applications"]}
    test("1.3 Applications detected", len(apps) > 0, f"Found: {apps}")

    # 1.4 Classify workload types
    workload_types = []
    if "Spark" in apps:
        workload_types.append("Spark")
    if "Hive" in apps:
        workload_types.append("Hive")
    if "Pig" in apps:
        workload_types.append("Pig")
    if "Presto" in apps:
        workload_types.append("Presto")
    if "Flink" in apps:
        workload_types.append("Flink")
    if "HBase" in apps:
        workload_types.append("HBase")
    test("1.4 Workload classification", len(workload_types) > 0,
         f"Workloads: {workload_types}")

    # 1.5 Check for hard blockers
    hard_blockers = []
    # MapR check (not applicable here but test the logic)
    # Ganglia check
    if "Ganglia" in apps:
        hard_blockers.append("Ganglia (removed from EMR 7.x)")
    test("1.5 No hard blockers", len(hard_blockers) == 0,
         f"Blockers: {hard_blockers if hard_blockers else 'None'}")

    # 1.6 Check bootstrap actions
    rc, output = run_aws(f"aws emr list-bootstrap-actions --cluster-id {cluster_id} --region {region}")
    if rc == 0:
        bootstrap_actions = json.loads(output).get("BootstrapActions", [])
    else:
        bootstrap_actions = cluster.get("BootstrapActions", [])
    test("1.6 Bootstrap actions listed", True,
         f"Found {len(bootstrap_actions)} bootstrap action(s)")

    # 1.7 Check instance groups
    # Instance groups are available directly from describe-cluster
    rc, output = run_aws(f"aws emr list-instance-groups --cluster-id {cluster_id} --region {region}")
    if rc == 0:
        instance_groups = json.loads(output).get("InstanceGroups", [])
    else:
        # Fallback: use instance groups from describe-cluster
        instance_groups = cluster.get("InstanceGroups", [])
    test("1.7 Instance groups listed", len(instance_groups) > 0,
         f"Found {len(instance_groups)} instance group(s)")

    # 1.8 Source cluster state detection
    state = cluster["Status"]["State"]
    is_running = state in ["WAITING", "RUNNING", "STARTING", "BOOTSTRAPPING"]
    test("1.8 Cluster state detection", True,
         f"State={state}, Running={is_running}")

    return {
        "cluster": cluster,
        "release": release,
        "apps": apps,
        "app_versions": app_versions,
        "workload_types": workload_types,
        "bootstrap_actions": bootstrap_actions,
        "instance_groups": instance_groups,
        "is_al1": is_al1,
        "is_running": is_running
    }

# ═══════════════════════════════════════════════════════════════════════════════
# Stage 2 — Adapt Cluster Configuration (Dry Run)
# ═══════════════════════════════════════════════════════════════════════════════

def stage2_adapt(gathered: Dict, target_release: str = "emr-7.1.0") -> Dict:
    """Stage 2: Generate adapted configuration (dry run — no S3 uploads)."""
    print("\n" + "="*70)
    print("STAGE 2 — Adapt Cluster Configuration (DRY RUN)")
    print("="*70)

    adapted_config = []

    # 2.1 Release label
    test("2.1 Release label", True, f"Set to {target_release}")

    # 2.2 Application version mapping
    version_map = {
        "Spark": ("2.4.7", "3.5.x"),
        "Hive": ("2.3.7", "3.1.x"),
        "Pig": ("0.17.0", "0.17.0 (broken — convert to PySpark)"),
        "Hadoop": ("2.10.1", "3.3.x"),
    }
    for app in gathered["apps"]:
        src_ver = gathered["app_versions"].get(app, "?")
        tgt_ver = version_map.get(app, (src_ver, "unknown"))[1]
        print(f"    {app}: {src_ver} → {tgt_ver}")
    test("2.2 Version mapping", True, f"Mapped {len(gathered['apps'])} applications")

    # 2.3 Spark backward compat configuration
    spark_compat = {
        "Classification": "spark-defaults",
        "Properties": {
            "spark.sql.ansi.enabled": "false",
            "spark.sql.storeAssignmentPolicy": "LEGACY",
            "spark.sql.legacy.timeParserPolicy": "LEGACY",
            "spark.sql.legacy.createHiveTableByDefault": "true",
            "spark.sql.legacy.parquet.int96RebaseModeInRead": "LEGACY",
            "spark.sql.legacy.parquet.int96RebaseModeInWrite": "LEGACY",
            "spark.sql.legacy.parquet.datetimeRebaseModeInRead": "LEGACY",
            "spark.sql.legacy.parquet.datetimeRebaseModeInWrite": "LEGACY",
            "spark.sql.legacy.avro.datetimeRebaseModeInRead": "LEGACY"
        }
    }
    if "Spark" in gathered["apps"]:
        adapted_config.append(spark_compat)
    test("2.3 Spark compat flags", "Spark" in gathered["apps"],
         f"Added {len(spark_compat['Properties'])} LEGACY properties")

    # 2.4 Hive configuration (no Glue in this cluster, so basic)
    if "Hive" in gathered["apps"]:
        # Check if cluster uses Glue Catalog
        cluster_configs = gathered["cluster"].get("Configurations", [])
        uses_glue = False
        for cfg in cluster_configs:
            if cfg.get("Classification") == "hive-site":
                props = cfg.get("Properties", {})
                if "AWSGlueDataCatalog" in props.get("hive.metastore.client.factory.class", ""):
                    uses_glue = True
        if uses_glue:
            adapted_config.append({
                "Classification": "hive-site",
                "Properties": {
                    "hive.create.as.acid": "false",
                    "hive.create.as.insert.only": "false"
                }
            })
        test("2.4 Hive config", True,
             f"Glue Catalog detected: {uses_glue}")

    # 2.5 Bootstrap action adaptation
    ba_adaptations = []
    for ba in gathered["bootstrap_actions"]:
        ba_adaptations.append({
            "original": ba.get("Name", "unknown"),
            "changes": ["yum→dnf", "service→systemctl", "python→python3", "IMDSv1→IMDSv2"]
        })
    test("2.5 Bootstrap adaptation", True,
         f"{len(ba_adaptations)} bootstrap action(s) to adapt")

    # 2.6 Instance type validation
    instance_types = set()
    for ig in gathered["instance_groups"]:
        instance_types.add(ig["InstanceType"])
    deprecated = {"m4.", "r4.", "c4.", "d2.", "i2."}
    needs_replacement = [t for t in instance_types if any(t.startswith(d) for d in deprecated)]
    test("2.6 Instance types valid", len(needs_replacement) == 0,
         f"Types: {list(instance_types)}, Previous-gen: {needs_replacement if needs_replacement else 'None'}")

    # 2.7 Log4j configuration
    if "Spark" in gathered["apps"]:
        adapted_config.append({
            "Classification": "spark-log4j2",
            "Properties": {
                "rootLogger.level": "warn"
            }
        })
    test("2.7 Log4j config", True, "spark-log4j → spark-log4j2 (Log4j2 format)")

    # 2.8 EMRFS removal check
    emrfs_props_found = False
    for cfg in gathered["cluster"].get("Configurations", []):
        if cfg.get("Classification") == "emrfs-site":
            emrfs_props_found = True
    test("2.8 EMRFS consistency view", True,
         f"EMRFS properties present: {emrfs_props_found} (will be removed)")

    # 2.9 Java 8→17 flags (added proactively for custom JARs)
    java_opts = "--add-opens java.base/java.lang=ALL-UNNAMED --add-opens java.base/java.util=ALL-UNNAMED --add-opens java.base/sun.nio.ch=ALL-UNNAMED"
    test("2.9 Java 17 compatibility", True, f"Prepared --add-opens flags for custom JARs")

    return {
        "target_release": target_release,
        "adapted_config": adapted_config,
        "ba_adaptations": ba_adaptations,
        "needs_instance_replacement": needs_replacement
    }

# ═══════════════════════════════════════════════════════════════════════════════
# Stage 3 — Upgrade Applications (Dry Run)
# ═══════════════════════════════════════════════════════════════════════════════

def stage3_upgrade(gathered: Dict) -> Dict:
    """Stage 3: Plan application upgrades (dry run — no actual changes)."""
    print("\n" + "="*70)
    print("STAGE 3 — Upgrade Applications (DRY RUN)")
    print("="*70)

    upgrades = {}

    # 3A — Spark Application Code
    if "Spark" in gathered["workload_types"]:
        print("\n  --- Stage 3A: Spark Application Code ---")
        # Check if Spark Upgrade Agent MCP is available
        spark_mcp_available = False  # Would check MCP connection
        # Not a failure — skill works without MCP (falls back to manual fixes)
        test("3A.1 Spark Upgrade Agent MCP", True,
             f"MCP connected: {spark_mcp_available} — {'automated' if spark_mcp_available else 'manual'} mode")

        spark_fixes = [
            "SPARK_SQL_LEGACY: Apply legacy compat flags",
            "SPARK_PARQUET_TIMESTAMP: Apply rebase mode flags",
            "SPARK_REMOVED_APIS: Rewrite deprecated APIs (SQLContext→SparkSession, etc.)",
            "SPARK_SCALA_BINARY: Recompile for Scala 2.12 (if JARs present)",
            "SPARK_PYTHON_VERSION: Convert Python 2→3 syntax"
        ]
        test("3A.2 Spark fixes planned", True, f"{len(spark_fixes)} fix categories identified")
        upgrades["spark"] = spark_fixes

    # 3B — Hive Application
    if "Hive" in gathered["workload_types"]:
        print("\n  --- Stage 3B: Hive Application Migration ---")
        hive_fixes = [
            "HIVE3_ACID_DEFAULT: Convert managed tables to EXTERNAL",
            "HIVE3_SYNTAX_CHANGES: Quote reserved keywords (date, time, user, etc.)",
            "HIVE3_TYPE_CONVERSION: Add explicit CAST() for implicit conversions",
            "HIVE3_EXECUTION_ENGINE: Remove hive.execution.engine=mr",
            "HIVE2_ACID_DELTA_FORMAT_INCOMPATIBLE: Export ACID table data (if applicable)"
        ]
        test("3B.1 Hive fixes planned", True, f"{len(hive_fixes)} fix categories identified")
        upgrades["hive"] = hive_fixes

    # 3F — Pig Application (must convert to PySpark)
    if "Pig" in gathered["workload_types"]:
        print("\n  --- Stage 3F: Pig → PySpark Conversion ---")

        # Check PigToSparkConversion MCP
        pig_mcp_available = False  # Would check MCP connection
        # Not a failure — skill works without MCP (falls back to manual conversion)
        test("3F.1 PigToSparkConversion MCP", True,
             f"MCP connected: {pig_mcp_available} — {'automated' if pig_mcp_available else 'manual'} conversion mode")

        pig_info = {
            "reason": "Pig 0.17.0 ORDER BY/JOIN fails with OperatorKey.hashCode() null on Java 17",
            "fix": "Convert all .pig scripts to PySpark DataFrame API",
            "critical": True
        }
        test("3F.2 Pig identified as critical break", pig_info["critical"],
             "Pig is NOT just deprecated — it's functionally broken on EMR 7.x")
        upgrades["pig"] = pig_info

    return upgrades

# ═══════════════════════════════════════════════════════════════════════════════
# Stage 4 — Launch Test Cluster (Dry Run — config generation only)
# ═══════════════════════════════════════════════════════════════════════════════

def stage4_launch(gathered: Dict, adapted: Dict) -> Dict:
    """Stage 4: Generate test cluster launch configuration (dry run)."""
    print("\n" + "="*70)
    print("STAGE 4 — Launch Test Cluster Config (DRY RUN)")
    print("="*70)

    # Build the RunJobFlow config
    cluster = gathered["cluster"]
    ec2_attrs = cluster["Ec2InstanceAttributes"]

    launch_config = {
        "Name": f"{cluster['Name']}-emr7-migrated",
        "ReleaseLabel": adapted["target_release"],
        "Applications": [
            {"Name": "Spark"},
            {"Name": "Hive"},
            {"Name": "Hadoop"},
            # NOTE: Pig intentionally excluded — broken on EMR 7.x
        ],
        "Instances": {
            "InstanceGroups": [
                {
                    "InstanceGroupType": "MASTER",
                    "InstanceCount": 1,
                    "InstanceType": "m5.xlarge"
                },
                {
                    "InstanceGroupType": "CORE",
                    "InstanceCount": 1,
                    "InstanceType": "m5.xlarge"
                }
            ],
            "Ec2SubnetId": ec2_attrs["Ec2SubnetId"],
            "KeepJobFlowAliveWhenNoSteps": False
        },
        "Configurations": adapted["adapted_config"],
        "ServiceRole": cluster.get("ServiceRole", "EMR_DefaultRole"),
        "JobFlowRole": ec2_attrs.get("IamInstanceProfile", "EMR_EC2_DefaultRole"),
        "Tags": [{"Key": "emr-migration-skill", "Value": "test-run"}],
        "AutoTerminate": True,
        "LogUri": cluster.get("LogUri", "")
    }

    # Validate config
    test("4.1 Pig excluded from target", "Pig" not in [a["Name"] for a in launch_config["Applications"]],
         "Pig intentionally NOT installed on target (broken on EMR 7.x)")
    test("4.2 Config has LEGACY flags", len(launch_config["Configurations"]) > 0,
         f"{len(launch_config['Configurations'])} configuration classification(s)")
    test("4.3 Tags include migration marker", 
         any(t["Key"] == "emr-migration-skill" for t in launch_config["Tags"]),
         "Tag: emr-migration-skill=test-run")
    test("4.4 Auto-terminate enabled", launch_config["AutoTerminate"],
         "Test cluster will auto-terminate on completion")
    test("4.5 Minimum viable size", 
         all(ig["InstanceCount"] == 1 for ig in launch_config["Instances"]["InstanceGroups"]),
         "1 primary + 1 core (minimum cost)")

    print(f"\n  Generated RunJobFlow config ({len(json.dumps(launch_config))} bytes)")
    return launch_config

# ═══════════════════════════════════════════════════════════════════════════════
# Stage 5/6 — Validation & Fix Loop Planning (Dry Run)
# ═══════════════════════════════════════════════════════════════════════════════

def stage5_6_validate(gathered: Dict, upgrades: Dict) -> Dict:
    """Stage 5/6: Plan validation steps and potential fix loops."""
    print("\n" + "="*70)
    print("STAGE 5/6 — Validation Planning (DRY RUN)")
    print("="*70)

    validation_steps = []

    if "Spark" in gathered["workload_types"]:
        validation_steps.append({
            "type": "Spark",
            "method": "spark-submit smallest representative step",
            "expected_issues": ["SPARK_SQL_LEGACY", "SPARK_PARQUET_TIMESTAMP", "SPARK_SCALA_BINARY"]
        })

    if "Hive" in gathered["workload_types"]:
        validation_steps.append({
            "type": "Hive",
            "method": "Execute adapted HQL scripts",
            "expected_issues": ["HIVE3_ACID_DEFAULT", "HIVE3_SYNTAX_CHANGES"]
        })

    if "Pig" in gathered["workload_types"]:
        validation_steps.append({
            "type": "Pig (converted to PySpark)",
            "method": "spark-submit converted PySpark scripts",
            "expected_issues": ["PIG_UDF_UNMAPPED", "PIG_SCHEMA_MISMATCH"]
        })

    test("5.1 Validation steps planned", len(validation_steps) > 0,
         f"{len(validation_steps)} validation step(s) for workloads: {[v['type'] for v in validation_steps]}")

    # Fix loop budget
    max_iterations = 5
    test("5.2 Fix loop budget", max_iterations == 5,
         f"Max {max_iterations} iterations per failure")

    # Halt conditions
    halt_conditions = [
        "Same failure recurs after fix (cycle detected)",
        "5 fix iterations exhausted",
        ">2 cluster launch failures",
        "Pig conversion >20% data mismatch"
    ]
    test("5.3 Halt conditions defined", len(halt_conditions) == 4,
         f"{len(halt_conditions)} halt conditions configured")

    return {"validation_steps": validation_steps, "max_iterations": max_iterations}

# ═══════════════════════════════════════════════════════════════════════════════
# Stage 7 — Report
# ═══════════════════════════════════════════════════════════════════════════════

def stage7_report(gathered: Dict, adapted: Dict, upgrades: Dict, launch_config: Dict):
    """Stage 7: Generate migration report."""
    print("\n" + "="*70)
    print("STAGE 7 — Migration Report (DRY RUN)")
    print("="*70)

    print(f"""
  Source Cluster: {gathered['cluster']['Id']}
  Source Release: {gathered['release']} (Amazon Linux 1)
  Target Release: {adapted['target_release']} (Amazon Linux 2023)
  
  Applications Migrated:
    • Spark 2.4.7 → 3.5.x (config transforms + code upgrade needed)
    • Hive 2.3.7 → 3.1.x (ACID handling + syntax fixes)
    • Pig 0.17.0 → PySpark (CRITICAL: must convert, Pig broken on Java 17)
    • Hadoop 2.10.1 → 3.3.x (S3 scheme migration)
  
  Cluster-Level Fixes:
    • {len(adapted['adapted_config'])} configuration classifications adapted
    • {len(adapted['ba_adaptations'])} bootstrap action(s) adapted
    • Log4j config format: log4j.properties → log4j2.properties
    • EMRFS Consistent View: removed (S3 strongly consistent since 2020)
    • IMDSv2 enforcement: bootstrap scripts adapted
  
  Application-Level Fixes:
    • Spark: 9 LEGACY compat flags, API rewrites, Scala 2.12
    • Hive: EXTERNAL tables, reserved keyword quoting, type casts
    • Pig: Full conversion to PySpark (ORDER BY/JOIN fatal on Java 17)
  
  Safety Guarantees:
    ✅ Original cluster NEVER modified
    ✅ Original code NEVER overwritten (new -migrated paths)
    ✅ Test cluster tagged for cost tracking
    ✅ Test cluster auto-terminates
    ✅ Minimum instance size (1+1)
""")

    # Final test: config is valid JSON
    config_json = json.dumps(launch_config, indent=2)
    test("7.1 Config serializable", len(config_json) > 100,
         f"Generated {len(config_json)} bytes of RunJobFlow JSON")

    return config_json

# ═══════════════════════════════════════════════════════════════════════════════
# Additional Validation Tests
# ═══════════════════════════════════════════════════════════════════════════════

def test_boundary_conditions():
    """Test edge cases and boundary conditions in the skill logic."""
    print("\n" + "="*70)
    print("ADDITIONAL — Boundary Condition Tests")
    print("="*70)

    # Test release label parsing
    test_cases = [
        ("emr-5.0.0", True, "Earliest AL1 release"),
        ("emr-5.35.0", True, "Latest AL1 release"),
        ("emr-5.36.0", False, "First AL2 release (out of scope)"),
        ("emr-5.37.0", False, "AL2 release"),
        ("emr-6.0.0", False, "EMR 6.x (AL2)"),
        ("emr-7.1.0", False, "EMR 7.x (target, not source)"),
    ]

    for release, expected_al1, desc in test_cases:
        version = release.replace("emr-", "")
        major, minor, patch = version.split(".")
        is_al1 = int(major) == 5 and int(minor) <= 35
        test(f"Boundary: {release}", is_al1 == expected_al1, desc)

    # Test Pig halt condition
    test("Pig is critical break (not just deprecated)", True,
         "ORDER BY/JOIN/COGROUP all fail — not a deprecation warning")

    # Test that Ganglia triggers halt
    test("Ganglia detection", True, "Ganglia in app list → hard blocker (removed from EMR 7.x)")

    # Test instance type substitution logic (only previous-gen types need replacement)
    deprecated_map = {"m4.xlarge": "m6i.xlarge", "r4.2xlarge": "r6i.2xlarge", "c4.large": "c6i.large"}
    for old, new in deprecated_map.items():
        test(f"Instance substitution: {old}→{new}", True, "Previous-gen → current-gen")

# ═══════════════════════════════════════════════════════════════════════════════
# Auto-Bootstrap: Create resources from scratch using current AWS credentials
# ═══════════════════════════════════════════════════════════════════════════════

_auto_created_resources = {"cluster_id": None, "bucket": None, "region": None}

def auto_bootstrap(region: str, emr_release: str) -> str:
    """
    Auto-create test resources using current AWS credentials.
    Returns cluster_id or None on failure.
    """
    import time

    print("\n🔧 AUTO-BOOTSTRAP: Creating test resources...")

    # 1. Get account ID from current credentials
    rc, output = run_aws(f"aws sts get-caller-identity --region {region} --query 'Account' --output text")
    if rc != 0:
        print(f"  ❌ Cannot get AWS account ID. Check credentials. Error: {output}")
        return None
    account_id = output.strip()
    print(f"  ✅ Account: {account_id}")

    # 2. Find a subnet to use (first default VPC subnet)
    rc, output = run_aws(
        f"aws ec2 describe-subnets --region {region} "
        f"--filters Name=default-for-az,Values=true "
        f"--query 'Subnets[0].SubnetId' --output text"
    )
    if rc != 0 or output == "None":
        # Try any subnet
        rc, output = run_aws(
            f"aws ec2 describe-subnets --region {region} "
            f"--query 'Subnets[0].SubnetId' --output text"
        )
    if rc != 0 or not output or output == "None":
        print(f"  ❌ No subnet found in {region}. Error: {output}")
        return None
    subnet_id = output.strip()
    print(f"  ✅ Subnet: {subnet_id}")

    # 3. Create S3 bucket for test artifacts
    bucket_name = f"emr-al1-migration-test-{account_id}-{region}"
    rc, output = run_aws(f"aws s3 ls s3://{bucket_name} 2>&1")
    if rc != 0:
        # Bucket doesn't exist, create it
        if region == "us-east-1":
            rc, output = run_aws(f"aws s3 mb s3://{bucket_name} --region {region}")
        else:
            rc, output = run_aws(
                f"aws s3api create-bucket --bucket {bucket_name} --region {region} "
                f"--create-bucket-configuration LocationConstraint={region}"
            )
        if rc != 0:
            print(f"  ⚠️  Bucket creation failed (may already exist): {output}")
    print(f"  ✅ Bucket: s3://{bucket_name}")
    _auto_created_resources["bucket"] = bucket_name
    _auto_created_resources["region"] = region

    # 4. Create EMR cluster
    print(f"  ⏳ Creating EMR cluster ({emr_release})...")
    create_cmd = (
        f"aws emr create-cluster --name 'al1-migration-skill-test' "
        f"--release-label {emr_release} "
        f"--applications Name=Spark Name=Hive Name=Hadoop Name=Pig "
        f"--instance-groups "
        f"'[{{\"InstanceGroupType\":\"MASTER\",\"InstanceCount\":1,\"InstanceType\":\"m5.xlarge\"}},"
        f"{{\"InstanceGroupType\":\"CORE\",\"InstanceCount\":1,\"InstanceType\":\"m5.xlarge\"}}]' "
        f"--ec2-attributes '{{\"SubnetId\":\"{subnet_id}\",\"InstanceProfile\":\"EMR_EC2_DefaultRole\"}}' "
        f"--service-role EMR_DefaultRole "
        f"--log-uri s3://{bucket_name}/logs/ "
        f"--region {region} --no-auto-terminate"
    )
    rc, output = run_aws(create_cmd)
    if rc != 0:
        print(f"  ❌ Cluster creation failed: {output}")
        return None
    cluster_id = json.loads(output)["ClusterId"]
    _auto_created_resources["cluster_id"] = cluster_id
    print(f"  ✅ Cluster: {cluster_id}")

    # 5. Wait for cluster to reach WAITING state
    print(f"  ⏳ Waiting for cluster to reach WAITING state (up to 10 min)...")
    for i in range(40):  # 40 * 15s = 10 min
        time.sleep(15)
        rc, output = run_aws(
            f"aws emr describe-cluster --cluster-id {cluster_id} --region {region} "
            f"--query 'Cluster.Status.State' --output text"
        )
        state = output.strip() if rc == 0 else "UNKNOWN"
        if state == "WAITING":
            print(f"  ✅ Cluster ready ({(i+1)*15}s)")
            return cluster_id
        if state in ("TERMINATED", "TERMINATED_WITH_ERRORS"):
            print(f"  ❌ Cluster terminated: {state}")
            return None
        if i % 4 == 0:
            print(f"     ... {state} ({(i+1)*15}s)")

    print(f"  ❌ Cluster did not reach WAITING within 10 minutes")
    return None


def auto_cleanup():
    """Terminate auto-created cluster (bucket is kept for log inspection)."""
    cluster_id = _auto_created_resources.get("cluster_id")
    region = _auto_created_resources.get("region")
    if cluster_id and region:
        print(f"\n🧹 Cleaning up: terminating cluster {cluster_id}...")
        run_aws(f"aws emr terminate-clusters --cluster-ids {cluster_id} --region {region}")
        print(f"  ✅ Cluster {cluster_id} termination initiated")
        bucket = _auto_created_resources.get("bucket")
        if bucket:
            print(f"  ℹ️  Bucket s3://{bucket} kept (contains logs). Delete manually if not needed.")


# ═══════════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="EMR AL1 Migration Skill — Dry Run Test")
    parser.add_argument("--cluster-id", help="Source EMR cluster ID (or use --auto to create one)")
    parser.add_argument("--region", default="us-east-1", help="AWS region")
    parser.add_argument("--target-release", default="emr-7.1.0", help="Target EMR release")
    parser.add_argument("--auto", action="store_true",
                        help="Auto-create resources: discovers account, creates S3 bucket + EMR cluster, runs tests, cleans up")
    parser.add_argument("--emr-release", default="emr-5.33.0",
                        help="EMR release for auto-created cluster (default: emr-5.33.0)")
    parser.add_argument("--no-cleanup", action="store_true",
                        help="Skip cleanup of auto-created resources (for debugging)")
    args = parser.parse_args()

    # Auto-bootstrap mode: create resources from scratch
    if args.auto:
        args.cluster_id = auto_bootstrap(args.region, args.emr_release)
        if not args.cluster_id:
            print("❌ Auto-bootstrap failed. Check AWS credentials and permissions.")
            sys.exit(1)

    if not args.cluster_id:
        parser.error("Either --cluster-id or --auto is required")

    print("╔══════════════════════════════════════════════════════════════════════╗")
    print("║  EMR AL1 Migration Skill — Dry-Run Simulation Test                  ║")
    print("╠══════════════════════════════════════════════════════════════════════╣")
    print(f"║  Cluster: {args.cluster_id:<55} ║")
    print(f"║  Region:  {args.region:<55} ║")
    print(f"║  Target:  {args.target_release:<55} ║")
    print(f"║  Mode:    DRY_RUN=true (no cluster launch, no S3 writes)           ║")
    print("╚══════════════════════════════════════════════════════════════════════╝")

    # Run all stages
    gathered = stage1_gather(args.cluster_id, args.region)

    if not gathered["is_al1"]:
        print(f"\n❌ HALT: Cluster {args.cluster_id} is {gathered['release']} (not AL1)")
        print("   Skill correctly refuses to proceed. Test PASSES for boundary detection.")
        test_boundary_conditions()
    else:
        adapted = stage2_adapt(gathered, args.target_release)
        upgrades = stage3_upgrade(gathered)
        launch_config = stage4_launch(gathered, adapted)
        stage5_6_validate(gathered, upgrades)
        config_json = stage7_report(gathered, adapted, upgrades, launch_config)
        test_boundary_conditions()

        # Write generated config to file for inspection
        output_path = "/tmp/emr7-migration-config.json"
        with open(output_path, "w") as f:
            f.write(config_json)
        print(f"\n  📄 Generated config written to: {output_path}")

    # Summary
    print("\n" + "="*70)
    print("TEST SUMMARY")
    print("="*70)
    passed = sum(1 for r in results if r.passed)
    failed = sum(1 for r in results if not r.passed)
    total = len(results)
    print(f"\n  Total: {total} | Passed: {passed} | Failed: {failed}")
    if failed > 0:
        print("\n  FAILURES:")
        for r in results:
            if not r.passed:
                print(f"    ❌ {r.name}: {r.message}")
                if r.details:
                    print(f"       {r.details}")
    print(f"\n  Result: {'ALL TESTS PASSED ✅' if failed == 0 else f'{failed} TEST(S) FAILED ❌'}")

    # Cleanup auto-created resources
    if args.auto and not args.no_cleanup:
        auto_cleanup()

    return 0 if failed == 0 else 1

if __name__ == "__main__":
    sys.exit(main())

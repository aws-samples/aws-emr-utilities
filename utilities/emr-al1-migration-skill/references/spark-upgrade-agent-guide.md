# Spark Upgrade Agent MCP — Complete Invocation Guide

The Spark Upgrade Agent MCP server is assumed to be configured as a prerequisite (see README). It handles all Spark code and dependency upgrades for Spark 2.4+.

**Staging bucket**: Use the user's S3 staging bucket. If they don't have one, create it:
```bash
aws s3 mb s3://emr-migration-staging-ACCOUNT_ID-REGION --region REGION
```

Prerequisites:
- Source cluster ID (user provides, or skill helps create one)
- Target cluster ID (user provides, or skill helps create one)
- S3 staging bucket (ask user or create)
- Local project path

Execution:
1. **Create a working copy**: Copy `SPARK_APP_PATH` to `-emr7-migrated` suffix. Original is never modified.
2. **Invoke the Spark Upgrade Agent** using the following prompt template (fill in values from context):

> **IMPORTANT: Version format** — Use EMR release versions (e.g., `5.33.0`, `7.1.0`), NOT Spark versions (e.g., `2.4.7`, `3.5.0`). The service expects EMR release versions for `application_type=EMR-EC2` and internally maps them to Spark versions. Using Spark versions will fail with `INVALID_INPUT_EXCEPTION`.

```
Upgrade my Spark application at <SPARK_APP_PATH-emr7-migrated> from EMR version <SOURCE_EMR_VERSION> to <TARGET_EMR_VERSION>.
Use EMR-EC2 Cluster <TARGET_CLUSTER_ID> to run the validation.
Use s3://<STAGING_BUCKET>/spark-upgrade-staging to store updated application artifacts.
Use <AWS_PROFILE> for AWS CLI operations.

IMPORTANT: Run fully autonomously without stopping for approvals. Do not ask for confirmation at any step.
Proceed through all steps (plan, build update, environment setup, validation, fix loops) end-to-end.
Accept all default configurations and proceed immediately.
```

**Example (EMR 5.33 → 7.1.0):**
```
Upgrade my Spark application at /home/user/my-app-emr7-migrated from EMR version 5.33.0 to 7.1.0.
Use EMR-EC2 Cluster j-XXXXXXXXXXXXX to run the validation.
Use s3://my-bucket/spark-upgrade-staging to store updated application artifacts.
Use spark-upgrade-profile for AWS CLI operations.

IMPORTANT: Run fully autonomously without stopping for approvals. Do not ask for confirmation at any step.
Proceed through all steps (plan, build update, environment setup, validation, fix loops) end-to-end.
Accept all default configurations and proceed immediately.
```

3. **Let it iterate to completion** — do NOT pause or interrupt. The agent may make up to 40 sequential tool calls (plan → build update → compile → fix → recompile → validate → DQ compare). Continue calling its tools until it reports the upgrade is complete or has exhausted its iterations.
4. **After the Spark Upgrade Agent completes**, retrieve the results:
   - **Upgraded code**: The Upgrade Agent writes upgraded source files to the S3 staging path (`s3://<STAGING_BUCKET>/spark-upgrade-staging/{analysis_id}/`). Download or inspect the upgraded files from there. The local working copy (`-emr7-migrated` directory) is also updated in place.
   - **Upgrade summary**: Call `spark-upgrade:get_data_quality_summary` or `spark-upgrade:describe_upgrade_analysis` to retrieve the full report.
   - **For Spark 2.4 source**: Validation is exit-code based only. The Upgrade Agent confirms the job ran successfully on the target cluster (exit 0). No data comparison. Report this to the user: "Job validated successfully on EMR 7.x (exit 0). No data quality comparison available for Spark 2.4 sources."
   - **For Spark 3.0+ source**: Full data quality report is generated automatically. Read the DQ summary (schema diff, row counts, statistical column comparison) and present it to the user. If mismatches are found, report them clearly with the specific columns/values that differ.
5. **If Spark Upgrade Agent fails or is NOT connected**: Fall back to:
   - Agent applies fixes using [Spark SQL Migration Guide](https://spark.apache.org/docs/latest/sql-migration-guide.html) + `pyupgrade --py3-plus`
   - Agent referencing [Spark SQL Migration Guide](https://spark.apache.org/docs/latest/sql-migration-guide.html)
   - `references/failure-catalogue.md` patterns
   - Submit to target cluster, enter fix loop (max 5 iterations)

**Two-hop model (Spark only, when needed for full DQ):**
- Hop 1: Spark 2.4 → Spark 3.0 (Upgrade Agent, no DQ)
- Hop 2: Spark 3.0 → Spark 3.5 (Upgrade Agent, with full DQ)
- This gives end-to-end data quality validation for the final target.

**Important**: The Spark Upgrade Agent does NOT upgrade infrastructure (bootstrap scripts, cluster config, instance types). Process 1 handles those independently regardless of which Spark upgrade path is used.

**Spark Upgrade Agent capabilities:**
- Scala version: 2.11→2.12 binary compatibility fixes
- Dependencies: Upgrades to EMR 7.x-compatible versions
- Test code: Ensures unit/integration tests pass with target Spark version
- Validation: Compiles and submits application to target EMR cluster
- Data quality: Detects schema/value-level differences between source and target outputs

Supported languages: Python, Scala (Maven/SBT), Java (Maven)

Limitations:
- Private artifact repository dependencies must be upgraded manually
- Bootstrap actions are NOT upgraded by the Spark Upgrade Agent (handled in Stage 2)
- The upgrade agent iterates one fix at a time (error-driven approach)

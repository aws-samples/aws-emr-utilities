# EMR AL1 → AL2023 Migration Skill

> **Skill file**: [`SKILL.md`](SKILL.md) is the contract the AI agent follows.
> This skill is customizable — fork and adapt to your organization's standards.

Upgrades Amazon EMR clusters from **EMR 5.x (Amazon Linux 1)** to **EMR 7.x (Amazon Linux 2023)**, including all application code: Spark, Hive, Presto→Trino, MapReduce, Flink, Pig→PySpark, and Zeppelin notebooks.

---

## Quick start

1. Place the skill where your agent can read it (Claude Code, Kiro, or any MCP-aware agent):

```bash
git clone https://github.com/aws-samples/aws-emr-utilities.git
cd aws-emr-utilities/utilities/emr-al1-migration-skill
# or copy emr-al1-migration-skill/ into your project's .kiro/skills/ or .claude/skills/ directory
```

2. Invoke via prompt:

```
Use skill emr-al1-migration to migrate EMR cluster <cluster-id>
from EMR 5.x to EMR 7.x in region <region> using profile <profile-name>.
```

3. **(Kiro only) Enable autonomous mode** — add all spark-upgrade tools to `autoApprove` in `~/.kiro/settings/mcp.json` so the Spark Upgrade Agent runs E2E without pausing for approval at each step. See [GETTING_STARTED.md](GETTING_STARTED.md) for the full config.

For batch migration of scripts without a running cluster, point the agent at your script files directly. See [Batch migration](#batch--headless-upgrade-at-scale) below.

---

## At a glance

| Dimension | Value |
|---|---|
| Source | EMR 5.0–5.35 (Amazon Linux 1, Java 8, Spark 2.4, Hive 2.3, Pig 0.17) |
| Target | EMR 7.x latest stable (Amazon Linux 2023, Java 17, Spark 3.5, Hive 3.1, Trino) |
| Strategy | New cluster — original never modified |
| Applications | Spark, Hive, Presto→Trino, MapReduce, Flink, Pig→PySpark, Zeppelin |
| Iteration cap | 5 run-fail-fix cycles per application |
| Revert | Skill does not modify source cluster; discard target cluster to revert |
| References | [`references/failure-catalogue.md`](references/failure-catalogue.md) — breaking-change taxonomy |

---

## Important constraints — read before running

1. **Pre-prod first. Always.** The skill launches a new test cluster and runs validation steps on it. It never modifies the source cluster, but it does incur EC2/EMR costs and writes output to S3.

2. **No data validation.** The skill verifies that jobs *complete successfully* (exit code 0). It does not compare output data correctness — that remains your responsibility.

3. **Migrated scripts are written to new locations — originals are never modified.** All adapted artifacts use a suffix naming convention so the original files remain untouched:

   | Application | Original | Migrated Output |
   |-------------|----------|-----------------|
   | Bootstrap scripts | `s3://bucket/path/script.sh` | `s3://bucket/path/script-emr7-migrated.sh` |
   | Hive scripts | `s3://bucket/path/query.hql` | `s3://bucket/path/query-hive3-migrated.hql` |
   | Presto/Trino scripts | `s3://bucket/path/query.sql` | `s3://bucket/path/query-trino-migrated.sql` |
   | MapReduce JARs | `s3://bucket/path/job.jar` | `s3://bucket/path/job-hadoop3-migrated.jar` |
   | Flink JARs | `s3://bucket/path/flink-app.jar` | `s3://bucket/path/flink-app-flink18-migrated.jar` |
   | Spark application code | `local-path/src/` | `local-path/src-emr7-migrated/` (full copy) |
   | Pig → PySpark | `s3://bucket/pig/script.pig` | `s3://bucket/converted/$DOMAIN/data_store/script_name.py` |
   | Zeppelin notebooks | `notebook_{id}.json` | `notebook_{id}_emr7_migrated.json` |

   Re-running the migration overwrites the `-migrated` artifacts (idempotent) but never touches originals.

4. **Pig is broken, not deprecated.** Pig 0.17.0 installs on EMR 7.x but crashes at runtime on ORDER BY/JOIN due to a Java 17 serialization bug. The skill converts Pig to PySpark — there is no "keep Pig running" option.

5. **IMDSv2 is enforced.** Bootstrap actions or scripts calling `curl http://169.254.169.254/...` (IMDSv1) will get HTTP 401. The skill flags these for token-based IMDSv2 conversion.

6. **Zeppelin %sh and %pig are gone.** Not disabled — fully removed from Zeppelin 0.11.1 (EMR 7.5+). No JARs, no directories, cannot be re-enabled. The skill converts `%pig` → `%pyspark` and recommends `%python` with `subprocess` for shell commands.

7. **s3n:// still works (for now).** The skill migrates `s3n://` → `s3://` as best practice, but `s3n://` has not been removed from EMR 7.5.

---

## How it works

1. **Inventory** — Identify source cluster configuration, applications, steps, and bootstrap actions.
2. **Cluster config migration** — Adapt instance types, configurations, security groups, and classifications for EMR 7.x.
3. **Application migration** — For each application type:
   - Spark: API upgrades (SQLContext→SparkSession, deprecated methods), s3n→s3
   - Hive: ACID handling (export to non-ACID), CREATE→CREATE EXTERNAL
   - Presto→Trino: CLI rename, function syntax changes
   - MapReduce: Python 2→3 shebangs, `print`/`dict` syntax
   - Flink: Deployment mode and memory config property renames
   - Pig: Full conversion to PySpark (Pig is broken on Java 17)
   - Zeppelin: Interpreter migration (%pig→%pyspark, %sh removal)
4. **Launch test cluster** — Create EMR 7.x cluster with migrated configuration.
5. **Validate** — Submit migrated scripts as EMR steps; monitor for completion.
6. **Diagnose failures** — Classify against [`failure-catalogue.md`](references/failure-catalogue.md) (30+ failure categories).
7. **Fix and rerun** — Apply targeted fix, resubmit (max 5 iterations), or escalate.

> **Encryption SDK path**: Jobs using the AWS Encryption SDK may require a two-hop migration (0.9/1.0 → 2.0 → 4.0 equivalent pattern for EMR security configuration changes).

---

## Prerequisites

### IAM permissions

| Service | Actions Required |
|---|---|
| EMR | `elasticmapreduce:Describe*`, `RunJobFlow`, `AddJobFlowSteps`, `ListSteps`, `TerminateJobFlows` |
| S3 | `s3:GetObject`, `s3:PutObject`, `s3:ListBucket` on source/target buckets |
| EC2 | `ec2:Describe*` (for VPC/subnet/security group lookup) |
| IAM | `iam:PassRole` for EMR service role and EC2 instance profile |
| CloudWatch | `logs:GetLogEvents` (for step log retrieval) |

See [`references/iam-permissions.md`](references/iam-permissions.md) for the complete policy document.

### Pre-flight checklist

- [ ] Running in a pre-prod/test account (or dedicated VPC)
- [ ] Source cluster ID and region identified
- [ ] S3 bucket for migrated artifacts and logs created
- [ ] EMR service role and EC2 instance profile configured
- [ ] Source scripts backed up separately (skill reads but never modifies originals)
- [ ] Spark Upgrade Agent MCP server configured (see below)
- [ ] Familiar with [Important constraints](#important-constraints--read-before-running)

### Spark Upgrade Agent MCP Setup (required for Spark 2.4+ code upgrades)

The Spark Upgrade Agent handles Spark application code upgrades, dependency resolution, and validation. Set it up before running the skill:

**1. Create IAM Role** with EMR + S3 permissions:

```bash
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
REGION=us-east-1  # your region

# Create role
aws iam create-role --role-name SparkUpgradeMCPRole \
  --assume-role-policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Principal\":{\"AWS\":\"arn:aws:iam::${ACCOUNT_ID}:root\"},\"Action\":\"sts:AssumeRole\"}]}"

# Attach EMR + S3 permissions
aws iam put-role-policy --role-name SparkUpgradeMCPRole \
  --policy-name SparkUpgradeAccess \
  --policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":[\"elasticmapreduce:DescribeCluster\",\"elasticmapreduce:DescribeStep\",\"elasticmapreduce:ListSteps\",\"elasticmapreduce:ListClusters\",\"elasticmapreduce:AddJobFlowSteps\",\"elasticmapreduce:CreatePersistentAppUI\",\"elasticmapreduce:DescribePersistentAppUI\",\"elasticmapreduce:GetPersistentAppUIPresignedURL\"],\"Resource\":\"*\"},{\"Effect\":\"Allow\",\"Action\":[\"s3:GetBucket*\",\"s3:GetObject*\",\"s3:List*\",\"s3:Put*\"],\"Resource\":[\"arn:aws:s3:::YOUR_STAGING_BUCKET\",\"arn:aws:s3:::YOUR_STAGING_BUCKET/*\"]}]}"
```

Reference: https://docs.aws.amazon.com/emr/latest/ReleaseGuide/emr-spark-upgrade-agent-iam-role.html

**2. Create S3 staging bucket** (for upgrade artifacts and summaries):

```bash
aws s3 mb s3://spark-upgrade-staging-${ACCOUNT_ID} --region ${REGION}
```

**3. Configure AWS CLI profile:**

```bash
aws configure set profile.spark-upgrade-profile.source_profile default
aws configure set profile.spark-upgrade-profile.role_arn arn:aws:iam::${ACCOUNT_ID}:role/SparkUpgradeMCPRole
aws configure set profile.spark-upgrade-profile.region ${REGION}
```

**4. Add to `~/.kiro/settings/mcp.json`:**

```json
{
  "mcpServers": {
    "spark-upgrade": {
      "type": "stdio",
      "command": "uvx",
      "args": [
        "mcp-proxy-for-aws@latest",
        "https://sagemaker-unified-studio-mcp.<REGION>.api.aws/spark-upgrade/mcp",
        "--service", "sagemaker-unified-studio-mcp",
        "--profile", "spark-upgrade-profile",
        "--region", "<REGION>",
        "--read-timeout", "180"
      ],
      "timeout": 180000
    }
  }
}
```

Replace `<REGION>` with your AWS region (e.g., `us-east-1`).

---

## Confirmed breaks on EMR 7.5

Validated via end-to-end testing on EMR 7.5 (AL2023):

| Break | Error | Severity |
|---|---|---|
| Pig ORDER BY/JOIN | `OperatorKey.hashCode()` NPE — Java 17 serialization failure | Critical |
| `presto-cli` | `command not found` — replaced by `trino-cli` | Critical |
| `#!/usr/bin/python` shebang | `PipeMapRed subprocess failed` — Python 2 gone | Critical |
| IMDSv1 metadata calls | HTTP 401 — IMDSv2 enforced | Critical |
| Hive ACID ORC files | `BytesColumnVector cannot be cast to LongColumnVector` | Critical |
| Zeppelin `%pig` | Interpreter fully removed | Critical |
| Zeppelin `%sh` | Interpreter fully removed (no JAR, cannot re-enable) | Critical |
| Spark ANSI mode (opt-in) | Overflow, division-by-zero, invalid casts throw exceptions | Medium |
| Spark `log4j.properties` | Format changed to `log4j2.properties` | Medium |

### What still works (backward compat)

| Pattern | Status on EMR 7.5 |
|---|---|
| `s3n://` scheme | Still works (deprecated, migrate anyway) |
| `registerTempTable()` | Still works (deprecated alias) |
| `unionAll()` | Still works (deprecated alias) |
| `SQLContext` | Still works (deprecated) |
| Flink `-m yarn-cluster` | Still recognized (prefer `-t yarn-application`) |
| Java reflection (`setAccessible`) | Works — EMR adds 15 `--add-opens` flags |

---

## Example: Pig script with ORDER BY

**Input**: A `.pig` file using LOAD, FILTER, GROUP BY, JOIN, ORDER BY

**What happens on EMR 7.5**:
```
ERROR 2998: Unhandled internal error.
java.io.IOException: Deserialization error: Cannot invoke
"org.apache.pig.impl.plan.OperatorKey.hashCode()" because "this.mKey" is null
```

**Skill action**: Converts the entire script to PySpark DataFrame operations. There is no fix for the Pig engine — Pig 0.17.0 (2017) is the final release and will never be patched for Java 17.

**Output**: Equivalent PySpark script using `spark.read.csv()`, `.filter()`, `.groupBy().agg()`, `.join()`, `.orderBy()`.

---

## Example: Presto query migration

**Input**: A SQL file using `presto-cli --execute "..."`

**Iteration 1 (FAILED)**:
```
presto-cli: command not found
```
Classification: `PRESTO_REMOVED`

**Skill fix**: Rewrites to `trino-cli --execute "..."`, updates function calls (`json_extract` → `json_extract_scalar` where needed).

**Iteration 2 (SUCCEEDED)**: Query completes on Trino.

---

## Batch / headless upgrade at scale

### Option 1 — Claude Code workflow orchestration

```
Migrate all scripts in s3://my-bucket/emr-scripts/ from EMR 5.x patterns to EMR 7.x.
Use skill emr-al1-migration. Process Spark, Hive, and MapReduce scripts in parallel.
Store migrated scripts in s3://my-bucket/emr-scripts-migrated/.
```

### Option 2 — Per-script sessions

```bash
# Claude Code CLI
claude -p "Use skill emr-al1-migration to migrate s3://bucket/script.py to EMR 7.x"

# Multiple scripts via loop
for script in $(aws s3 ls s3://bucket/scripts/ --recursive | awk '{print $4}'); do
  claude -p "Use skill emr-al1-migration to migrate s3://bucket/$script to EMR 7.x. \
    Store output in s3://bucket/migrated/$script"
done
```

---

## Out of scope

- Data validation / output correctness comparison
- Auto-scaling policy migration (recreate manually)
- EMR on EKS or EMR Serverless (this skill targets EMR on EC2)
- HBase migration (use the `hbase-emr` skill)
- Glue ETL jobs (use the `glue-v09-v1-migration` skill)
- Change management / approval workflows

---

## Trigger phrases

- "migrate EMR cluster from AL1 to AL2023"
- "upgrade EMR 5 to EMR 7"
- "convert Pig script to PySpark for EMR 7"
- "fix Presto script for Trino on EMR 7"
- "migrate Zeppelin notebook to EMR 7"

---

## References

| Resource | Location |
|---|---|
| Skill contract | [`SKILL.md`](SKILL.md) |
| Failure catalogue | [`references/failure-catalogue.md`](references/failure-catalogue.md) |
| Pig→Spark mapping | [`references/pig-to-spark-mapping.md`](references/pig-to-spark-mapping.md) |
| Configuration transforms | [`references/configuration-transforms.md`](references/configuration-transforms.md) |
| Removed applications | [`references/removed-applications.md`](references/removed-applications.md) |
| Zeppelin interpreter migration | [`references/zeppelin-interpreter-migration.md`](references/zeppelin-interpreter-migration.md) |
| IAM permissions | [`references/iam-permissions.md`](references/iam-permissions.md) |
| AWS EMR Migration Guide | [docs.aws.amazon.com/emr/latest/ManagementGuide](https://docs.aws.amazon.com/emr/latest/ManagementGuide/emr-plan-migration.html) |
| EMR Release Guide | [docs.aws.amazon.com/emr/latest/ReleaseGuide](https://docs.aws.amazon.com/emr/latest/ReleaseGuide/emr-release-components.html) |

---

## Final reminder

**Always test in pre-prod first.** The skill creates new clusters and writes to S3 — it never modifies your source cluster or scripts. Validate migrated job outputs match your expectations before promoting to production. Retain source scripts and cluster configuration as rollback path.
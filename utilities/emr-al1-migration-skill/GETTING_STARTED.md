# Getting Started with the EMR AL1 Migration Skill

This guide walks you through setup, usage, expected time investment, use cases, and feedback collection for the EMR 5.x → 7.x migration skill.

---

## 1. Steps to Setup

### Prerequisites (one-time, ~15 minutes)

| Step | Action | Time |
|------|--------|------|
| 1 | Install an AI agent (pick one): [Kiro](https://kiro.dev), [Claude Code](https://code.claude.com), or [Codex CLI](https://platform.openai.com/docs/guides/codex) | 5 min |
| 2 | Clone this repository to your local machine | 1 min |
| 3 | Configure AWS credentials with [required IAM permissions](references/iam-permissions.md) | 5 min |
| 4 | (Optional) Set up [Spark Upgrade Agent MCP](https://docs.aws.amazon.com/emr/latest/ReleaseGuide/emr-spark-upgrade-agent-setup.html) for automated Spark code upgrades | 10 min |
| 5 | (Optional) Set up PigToSparkConversion MCP for automated Pig→PySpark conversion | 10 min |

### Installation

```bash
# Step 1: Clone the repo
git clone https://github.com/aws-samples/aws-emr-utilities.git
cd aws-emr-utilities/utilities/emr-al1-migration-skill

cd emr-al1-migration-skill

# Step 2: Configure AWS credentials (use a non-production account)
# Option A: Use an AWS CLI named profile
aws configure --profile emr-migration
# Set: AWS Access Key ID, Secret Access Key, Default region (e.g., us-east-1)

# Option B: Set environment variables directly
export AWS_ACCESS_KEY_ID=<your-access-key>
export AWS_SECRET_ACCESS_KEY=<your-secret-key>
export AWS_DEFAULT_REGION=us-east-1

# Option C: Use IAM role assumption (recommended for cross-account)
aws sts assume-role --role-arn arn:aws:iam::<ACCOUNT_ID>:role/<ROLE_NAME> \
  --role-session-name emr-migration

# You need a role with permissions listed in references/iam-permissions.md
export AWS_PROFILE=<your-profile>

# Step 3: Verify access
aws emr list-clusters --region <REGION> --query 'Clusters[?Status.State==`WAITING`]'
```

### For Kiro users (workspace skill)

Copy the skill directly into your Kiro workspace:

```bash
mkdir -p .kiro/skills/emr-al1-migration
cp -r emr-al1-migration-skill/* .kiro/skills/emr-al1-migration/
```

Then invoke by typing in chat:
```
migrate my EMR cluster j-XXXXX in us-east-1 to EMR 7.x
```

### For Claude Code / Codex CLI users

Reference the skill file directly:
```
use the skill at ./emr-al1-migration-skill/SKILL.md
to migrate my EMR 5.33 cluster j-XXXXX in us-east-1 to EMR 7.x.
use aws profile <profile-name> for any aws call.
```

---

## 2. Resources — Skills and References

### Core Skill File

| File | Purpose | When to Read |
|------|---------|--------------|
| [SKILL.md](SKILL.md) | Agent contract — the full migration workflow | Agent reads this automatically |
| [README.md](README.md) | Human documentation — overview, constraints, examples | Before first use |

### Reference Files (used by the agent during migration)

| File | Purpose | Categories |
|------|---------|-----------|
| [failure-catalogue.md](references/failure-catalogue.md) | 44 failure categories with log patterns and fixes | Platform, Spark, Hive, Hadoop, Presto, Flink |
| [configuration-transforms.md](references/configuration-transforms.md) | Property-by-property mapping from EMR 5.x → 7.x | Config classification changes |
| [pig-to-spark-mapping.md](references/pig-to-spark-mapping.md) | Pig Latin operator → PySpark/Spark SQL conversion | LOAD, FILTER, JOIN, GROUP, UDFs |
| [zeppelin-interpreter-migration.md](references/zeppelin-interpreter-migration.md) | Zeppelin notebook interpreter config changes | Spark, Hive, Shell interpreters |
| [removed-applications.md](references/removed-applications.md) | Migration paths for Pig, Oozie, Ganglia | Alternatives and conversion guides |
| [iam-permissions.md](references/iam-permissions.md) | Required IAM actions | EMR, S3, CloudWatch, IAM |

### Optional MCP Servers (enhanced automation)

| MCP Server | What It Does | Setup Time |
|------------|-------------|-----------|
| **Spark Upgrade Agent** | Automated Spark code upgrades (build file updates, API replacements, compilation, validation) | 10 min |
| **PigToSparkConversion** | AST-based Pig→PySpark conversion with test generation | 10 min |

---

## 3. Time Investment

### Per-Cluster Migration Time

| Complexity | Description | Estimated Time | Agent Effort | Human Effort |
|-----------|-------------|----------------|--------------|--------------|
| **Simple** | Spark-only cluster, no custom JARs, no Pig | 15–30 min | Fully automated | Review output |
| **Medium** | Spark + Hive, bootstrap scripts, some custom config | 30–60 min | Mostly automated, 1–2 fix iterations | Review + validate data |
| **Complex** | Spark + Hive + Pig + custom UDFs + streaming | 1–3 hours | Automated conversion + manual UDF review | Review + rewrite complex UDFs + validate |
| **Very Complex** | Multiple custom JARs (Scala 2.11), Oozie workflows, HBase | 3–8 hours | Partial automation | Significant manual effort for JARs and orchestration redesign |

### Breakdown by Phase

| Phase | Time | What Happens |
|-------|------|-------------|
| Gather cluster info | 2 min | Agent reads cluster config via AWS APIs |
| Adapt configuration | 3 min | Agent applies config transforms |
| Upgrade applications | 5–60 min | Depends on workload count and complexity |
| Launch test cluster | 5–8 min | EMR cluster startup time |
| Validate + fix loop | 5–30 min | Per iteration: run step (~2 min) + diagnose + fix |
| Report | 1 min | Final config JSON + fix summary |

### Total for a Fleet

| Fleet Size | Simple Clusters | Mixed Complexity |
|-----------|-----------------|-----------------|
| 5 clusters | 1–2 hours | 3–5 hours |
| 20 clusters | 4–8 hours (parallel) | 1–2 days |
| 50+ clusters | 1–2 days (batch) | 3–5 days |

---

## 4. What Do We Want Users to Do?

### Primary Goal

**Use the skill to migrate at least one real EMR 5.x cluster to EMR 7.x** and report what worked, what didn't, and where human intervention was needed.

### Specific Actions

| # | Action | Expected Outcome |
|---|--------|-----------------|
| 1 | **Run the skill against a non-prod EMR 5.x cluster** | Successful EMR 7.x test cluster with all workloads validated |
| 2 | **Validate the output** | Confirm data correctness (row counts, schema, business rules) |
| 3 | **Note any failures the skill couldn't fix** | Identify gaps in the failure catalogue |
| 4 | **Test with different workload types** | Spark, Hive, Pig, Presto — the more variety, the better |
| 5 | **Customize SKILL.md for your environment** | Add org-specific constraints, IAM boundaries, naming conventions |
| 6 | **Provide feedback** (see Section 6) | Help us improve the skill for all users |

### What NOT to Do

- ❌ Do NOT run directly against production clusters
- ❌ Do NOT skip data validation (step succeeded ≠ correct output)
- ❌ Do NOT assume one successful test means all clusters will migrate cleanly

### Important: Migrated Artifacts Are Never Written In-Place

The skill **never overwrites or replaces original scripts, JARs, or notebooks**. All migrated artifacts are written to new S3 locations using a suffix naming convention. This ensures originals remain untouched and rollback is always possible.

| Application | Original | Migrated Output |
|-------------|----------|-----------------|
| Bootstrap scripts | `s3://bucket/path/script.sh` | `s3://bucket/path/script-emr7-migrated.sh` |
| Hive scripts | `s3://bucket/path/query.hql` | `s3://bucket/path/query-hive3-migrated.hql` |
| Presto/Trino scripts | `s3://bucket/path/query.sql` | `s3://bucket/path/query-trino-migrated.sql` |
| MapReduce JARs | `s3://bucket/path/job.jar` | `s3://bucket/path/job-hadoop3-migrated.jar` |
| MapReduce streaming scripts | `s3://bucket/path/mapper.py` | `s3://bucket/path/mapper-hadoop3-migrated.py` |
| Flink JARs | `s3://bucket/path/flink-app.jar` | `s3://bucket/path/flink-app-flink18-migrated.jar` |
| Flink config | `s3://bucket/path/flink-conf.yaml` | `s3://bucket/path/flink-conf-flink18-migrated.yaml` |
| Spark application code | `local-path/src/` | `local-path/src-emr7-migrated/` (full copy) |
| Pig → PySpark | `s3://bucket/pig/script.pig` | `s3://bucket/converted/$DOMAIN/data_store/script_name.py` |
| Zeppelin notebooks | `notebook_{id}.json` | `notebook_{id}_emr7_migrated.json` |

If a migration is re-run, the `-migrated` artifacts are overwritten (idempotent), but originals are never affected.

---

## 5. Use Cases to Cover

### Priority 1 — Core Migration (cover these first)

| Use Case | Input | Expected Result |
|----------|-------|-----------------|
| **PySpark job migration** | EMR 5.33 cluster running PySpark 2.4 steps | Script upgraded for Spark 3.5 (removed APIs, ANSI mode, timestamps) |
| **Hive DDL/DML migration** | EMR 5.x cluster with HQL scripts | Scripts adapted for Hive 3.1 (EXTERNAL tables, reserved keywords, explicit CAST) |
| **Bootstrap action adaptation** | Cluster with AL1-specific bootstrap (yum, python, service) | Bootstrap rewritten for AL2023 (dnf, python3, systemctl) |

### Priority 2 — Application Conversions

| Use Case | Input | Expected Result |
|----------|-------|-----------------|
| **Presto → Trino** | Cluster with Presto queries/JDBC connections | Queries + connections renamed for Trino |
| **Pig → PySpark** | Cluster with Pig Latin scripts | Scripts converted to PySpark classes |
| **MapReduce → Spark** | Cluster with Hadoop streaming or MR JAR steps | Steps adapted or flagged for conversion |

### Priority 3 — Complex Scenarios

| Use Case | Input | Expected Result |
|----------|-------|-----------------|
| **Custom Scala JAR (2.11)** | Spark step with custom JAR compiled for Scala 2.11 | JAR decompiled, rewritten for 2.12, recompiled |
| **Hive ACID table migration** | Cluster with transactional tables on external RDS metastore | Compaction + schema upgrade + EXTERNAL table recreation |
| **Multi-application cluster** | Spark + Hive + Pig + Flink on same cluster | All applications migrated in sequence |

### Priority 4 — Scale Testing

| Use Case | Input | Expected Result |
|----------|-------|-----------------|
| **Batch migration** | 5+ clusters via headless/workflow mode | Parallel migration with per-cluster reports |
| **Diverse instance types** | Clusters using deprecated instance types (m4, r4, i2) | Instance types mapped to current-gen equivalents |

---

## 6. Feedback Collection

### Where to Submit Feedback

| Channel | URL | When to Use |
|---------|-----|-------------|
| **GitHub Issues** | `https://github.com/aws-samples/aws-emr-utilities/issues` | Bug reports, feature requests, failure catalogue gaps |
| **GitHub Discussions** | `https://github.com/aws-samples/aws-emr-utilities/discussions` | Questions, suggestions, share migration stories |
| **Quip Doc** (internal) | _[Link TBD — for internal AWS testers]_ | Internal feedback during beta |

### What to Include in Feedback

#### For Bug Reports / Failures

```markdown
## Bug Report

**Cluster details:**
- EMR version: (e.g., emr-5.33.0)
- Region: (e.g., us-east-1)
- Applications: (e.g., Spark 2.4, Hive 2.3, Presto)
- Instance types: (e.g., m5.xlarge primary, r5.2xlarge core)

**What happened:**
- Step in the skill workflow where it failed (e.g., Stage 3B — Hive migration)
- Error message or log snippet
- What the agent tried and why it didn't work

**Expected behavior:**
- What should have happened

**Workaround (if any):**
- What you did to get past the issue manually

**Failure category (if identifiable):**
- e.g., HIVE3_ACID_DEFAULT, SPARK_REMOVED_APIS, or "new — not in catalogue"

**Cluster ID (optional, for reproduction):**
- j-XXXXXXXXXXXXX

**Agent used:**
- Kiro / Claude Code / Codex CLI (version if known)
```

#### For Feature Requests

```markdown
## Feature Request

**Use case:**
- What you're trying to migrate that isn't covered

**Current behavior:**
- What the skill does (or doesn't do) today

**Desired behavior:**
- What you want it to do

**Priority:**
- Blocker (can't migrate without this) / Nice-to-have / Enhancement

**Workaround:**
- How you handled it manually (if applicable)
```

#### For Success Stories

```markdown
## Migration Success

**Source cluster:** EMR version, applications, cluster size
**Target cluster:** EMR 7.x version achieved
**Time taken:** End-to-end (setup to validated migration)
**Fix iterations:** How many cycles the agent needed
**Fixes applied:** List of failure categories triggered and resolved
**Human intervention needed:** What you had to do manually (if anything)
**Data validation:** How you confirmed correctness
**Would you use this for production migration?** Yes / No / With caveats
```

### Feedback Metrics We're Tracking

| Metric | What It Tells Us |
|--------|-----------------|
| Success rate | % of clusters fully migrated without human intervention |
| Fix iterations | Average cycles needed per cluster |
| Catalogue coverage | % of failures matched to existing categories vs. new/unknown |
| Time to migrate | Average wall-clock time per cluster by complexity |
| Agent type | Which agents (Kiro/Claude/Codex) work best |
| Human effort | Hours of manual work after skill completes |
| False positives | Cases where the skill made an incorrect fix |
| Missing categories | New failure types not in the catalogue |

---

## Quick Reference Card

```
┌─────────────────────────────────────────────────────┐
│           EMR AL1 Migration Skill                    │
├─────────────────────────────────────────────────────┤
│ Setup:     Clone repo + AWS creds         (15 min)  │
│ Invoke:    "migrate cluster j-XXX to 7.x"           │
│ Time:      15 min (simple) → 3 hrs (complex)        │
│ Strategy:  Gather → Adapt → Upgrade → Test → Fix    │
│ Safety:    Original cluster never modified           │
│ Feedback:  GitHub Issues + Discussions               │
├─────────────────────────────────────────────────────┤
│ Cover:     Spark, Hive, Presto, MR, Flink, Pig      │
│ Target:    EMR 7.x (Amazon Linux 2023)              │
│ Source:    EMR 5.0–5.35 (Amazon Linux 1)            │
└─────────────────────────────────────────────────────┘
```

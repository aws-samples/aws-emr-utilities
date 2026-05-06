# 🚀 OrionIQ — AI-Powered Data Platform Advisor

> Analyzes your real workloads, clusters, and costs — combined with a customizable knowledge base where you can add your own best practices, blog posts, and deployment guides — to recommend the optimal data platform deployment backed by actual metrics, not just keyword matching.

**Current focus:** AWS EMR (EC2, EKS, Serverless) and AWS Glue

## 🎬 Demo

[📹 Watch the demo](demo1.mp4) — Full walkthrough of OrionIQ analyzing a live EMR cluster and producing an evidence-based deployment recommendation.

## Why OrionIQ?

An LLM with cloud docs can say *"EMR Serverless is good for batch."* OrionIQ says:

> *"Your cluster is 90% idle with 4× m5.4xlarge instances running 1.3 minutes of actual compute. EMR Serverless costs $0.03 vs $6.11 on EC2 — switch to pay-per-use and eliminate idle cluster waste."*

That requires three things no general-purpose LLM has:
1. **Real metrics** from your actual cluster and Spark jobs
2. **Live pricing** from the AWS Pricing API
3. **Customizable knowledge base** — bring your own best practices, blog posts, and deployment guides. Add them to `config.yaml` and they're indexed and searchable at analysis time

## Getting Started

### Prerequisites

- Python 3.10+
- [uv](https://docs.astral.sh/uv/) package manager
- AWS credentials configured (`aws configure` or `AWS_PROFILE`)
- Access to an EMR cluster you want to analyze
- **Optional:** [Spark History Server MCP](https://github.com/kubeflow/mcp-apache-spark-history-server) — required only for app-ID-only analysis (no cluster ID). Not needed when analyzing EMR clusters directly.

### Installation

```bash
git clone https://github.com/aws-samples/aws-emr-utilities.git
cd aws-emr-utilities/mcp-servers/orioniq-spark-deployment-option-recommender
uv sync
```

### Usage Option 1: As an MCP Server (Kiro CLI / Claude Desktop)

OrionIQ runs as an MCP server that any MCP-compatible client can connect to (Kiro CLI, Claude Desktop, VS Code, etc.). Add it to your MCP client config:

```json
{
  "mcpServers": {
    "orioniq": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/orioniq-spark-deployment-option-recommender", "src/combined_emr_advisor_mcp.py"],
      "env": {
        "FASTMCP_LOG_LEVEL": "WARNING",
        "AWS_PROFILE": "your-profile"
      }
    }
  }
}
```

Config file location: `~/.kiro/settings/mcp.json` (Kiro CLI) or `claude_desktop_config.json` (Claude Desktop).

Then ask:
> *"Analyze EMR cluster j-XXXXXXXXXXXXX in us-east-1 and recommend the best deployment option"*

The agent will use OrionIQ's tools to gather data, analyze patterns, fetch pricing, and produce an evidence-based recommendation.

### Usage Option 2: Standalone Agent (Strands)

Run OrionIQ as an interactive agent with a conversation loop:

```bash
# Edit config.yaml with your cluster details, then:
uv run src/emr_advisor_agent_v2.py
```

This starts a Strands agent that connects to OrionIQ MCP + external MCP servers and lets you chat interactively. Requires:
- [Strands Agents](https://github.com/strands-agents/strands-agents) (`uv add strands-agents`)
- AWS Bedrock access with `anthropic.claude-3-5-sonnet` model enabled
- See `config.yaml` for model and MCP server configuration

### Configuration

Edit `config.yaml` or use environment variables:

```bash
export AWS_REGION="us-east-1"
export EMR_CLUSTER_ID="j-YOUR-CLUSTER-ID"
export EMR_CLUSTER_ARN="arn:aws:elasticmapreduce:us-east-1:123456789012:cluster/j-YOUR-CLUSTER-ID"
```

See [mcp_configuration_examples.md](mcp_configuration_examples.md) for advanced configuration options including custom knowledge sources, Docker deployment, and programmatic integration.

### Usage Option 3: Standalone CLI (No LLM Required)

Run OrionIQ directly from the command line — no Bedrock, no Kiro, no MCP client needed. Just Python + AWS credentials.

```bash
# Analyze an EMR cluster
uv run python3 orioniq_cli.py --cluster j-XXXXX --region us-east-1

# Include Glue in comparison
uv run python3 orioniq_cli.py --cluster j-XXXXX --region us-east-1 --include-glue

# With Kubernetes experience + HTML report
uv run python3 orioniq_cli.py --cluster j-XXXXX --region us-east-1 --k8s-experience --report

# Raw JSON output for scripting/pipelines
uv run python3 orioniq_cli.py --cluster j-XXXXX --region us-east-1 --json
```

**CLI Limitations:**
- No conversational interface — you provide all inputs upfront via flags
- No follow-up questions ("what if I had K8s experience?") — rerun with different flags
- No Spark History Server MCP integration for standalone app ID analysis — cluster-based analysis only (the tool fetches Spark data directly via EMR Persistent App UI)
- No knowledge base search — the curated blog/PDF insights are not loaded in CLI mode
- Same deterministic computation as the MCP server — identical numbers for the same inputs

## Example Prompts & Results

### Scenario 1: Short batch jobs on an oversized cluster → Serverless

**Prompt:**
```
Analyze my cluster j-XXXXXXXXXXXXX and the jobs running on the cluster and recommend me the right deployment option to use.
```
**User input:** `us-east-1, cost optimization, no kubernetes`

**Result:** The tool discovered 3 tiny Pi estimation jobs (1.3 min total) running on 4× m5.4xlarge instances with 90% idle time.

| Deployment Option | Cost | Score |
|---|---|---|
| **EMR Serverless** ✅ | **$0.05** | **87** |
| EMR on EC2 (Spot) | $0.27 | 30 |
| EMR on EKS (Spot) | $0.25 | 28 |
| EMR on EC2 (On-Demand) | $0.57 | 30 |
| EMR on EKS (On-Demand) | $0.54 | 28 |

**Why:** Pay-per-use eliminates idle cluster waste. Serverless charges only for the 1.3 minutes of actual compute. Even with spot pricing, EC2/EKS can't beat Serverless for sub-minute jobs.

### Scenario 2: Long IO-heavy job, no Kubernetes → EC2 Spot

**Prompt:**
```
Analyze my Spark application application_XXXX_YYYY on my local Spark History Server and recommend the right deployment option.
```
**User input:** `us-east-1, cost optimization, no kubernetes`

**Result:** The tool analyzed a 52-minute ETL job with 29 peak executors, 330GB shuffle, and 91% cluster utilization.

| Deployment Option | Cost | Score |
|---|---|---|
| **EMR on EC2 (Spot)** ✅ | **$4.76** | **67** |
| EMR on EKS (Spot) | $4.42 | 70 |
| EMR Serverless | $6.94 | 47 |
| EMR on EKS (On-Demand) | $9.76 | 28 |
| EMR on EC2 (On-Demand) | $10.10 | 43 |

**Why:** Long-running IO-heavy workloads are cheaper on EC2 with spot instances. EKS Spot is cheapest on cost but penalized due to no Kubernetes experience — the hard constraint caps its score below EC2.

### Scenario 3: Same job, with Kubernetes experience → EKS Spot

Same prompt as Scenario 2, but user has Kubernetes experience.

| Deployment Option | Cost | Score |
|---|---|---|
| **EMR on EKS (Spot)** ✅ | **$4.42** | **70** |
| EMR on EC2 (Spot) | $4.76 | 67 |
| EMR Serverless | $6.94 | 47 |
| EMR on EKS (On-Demand) | $9.76 | 28 |
| EMR on EC2 (On-Demand) | $10.10 | 43 |

**Why:** EKS Spot is cheapest ($0.34 less than EC2 Spot) because the EMR on EKS per-vCPU uplift is lower than the EMR on EC2 per-instance uplift. EKS also provides superior spot management (multi-AZ, graceful pod draining).

## Architecture

```
User Request
    │
    ▼
┌─────────────────────────────────────┐
│   AI Agent (Kiro CLI / Claude /     │
│   Strands / any MCP client)         │
│                                     │
│   Orchestrates the workflow:        │
│   - Asks user for priorities        │
│   - Calls tools in order            │
│   - Presents results                │
└──────────┬──────────────────────────┘
           │ calls tools via MCP
           ▼
┌─────────────────────────────────────┐
│   OrionIQ MCP Server                │
│   (self-contained, 9 tools)         │
│                                     │
│   Calls directly via boto3:         │
│   - EMR API (clusters, instances)   │
│   - EC2 API (spot prices)           │
│   - Pricing API (on-demand rates)   │
│   - EMR Persistent App UI (SHS)     │
└─────────────────────────────────────┘
```

### MCP Tools (9)

| Tool | Step | What it does |
|------|------|-------------|
| `generate_analysis_plan` | 0 | Presents analysis plan, collects user priorities. Resets state between analyses. |
| `load_knowledge_sources` | — | Indexes curated blogs, PDFs, and best practices docs |
| `search_knowledge_base` | — | Semantic search over expert content |
| `collect_workload_context` | 1 | Gathers user constraints + knowledge insights. No scoring — just context. |
| `analyze_spark_job_config` | 2 | Extracts Spark config from event logs or History Server |
| `analyze_cluster_patterns` | 3 | Analyzes EMR cluster via direct boto3 (idle time, spot usage, autoscaling). Discovers Spark apps and fetches executor data from SHS. |
| `get_emr_pricing` | 4 | Fetches live AWS pricing (on-demand + spot) + produces weighted evidence-based recommendation. Includes Glue pricing when triggered. |
| `generate_report` | — | Generates deterministic HTML report from analysis results. On-demand only — call when user asks for a report. |
| `analyze_glue_job` | — | Analyzes an existing Glue job and estimates EMR costs for migration comparison. |

### Evidence-Based Scoring

The final recommendation weighs four factors from real data. Weights adjust based on user priorities — if you say "performance matters most," workload fit gets 50% instead of 30%.

| Factor | Default | Cost priority | Performance priority | Operations priority |
|--------|---------|--------------|---------------------|-------------------|
| Cost | 40% | **60%** | 15% | 20% |
| Workload fit | 30% | 20% | **50%** | 15% |
| Operational fit | 20% | 15% | 20% | **50%** |
| Constraints | 10% | 5% | 15% | 15% |

When you pick two priorities, the weights are averaged. When you pick none, defaults apply.

## Testing

### Eval Framework

OrionIQ includes a 4-layer eval framework that validates computation determinism, workflow compliance, recommendation correctness, and cost accuracy — no AWS credentials or LLM needed:

```bash
uv run python3 evals/test_evals.py
```

| Layer | What it tests | Tests |
|-------|--------------|-------|
| **1. Computation Determinism** | Sweep-line executor algorithm, cost calculations, scoring engine — same input always produces same output | 13 |
| **2. Workflow Compliance** | Tool-level guards reject out-of-order calls, allow correct order, return actionable errors | 7 |
| **3. Recommendation Correctness** | Golden answers: wasteful→Serverless, efficient→EC2, spot-heavy+K8s→EKS, streaming→not Serverless | 4 |
| **4. Adversarial Cost Validation** | Independent recalculation verifies tool math, per-second billing confirmed, mixed instance types correct | 7 |

## Project Structure

```
src/
├── combined_emr_advisor_mcp.py      # MCP server with all tools
├── emr_advisor_agent_v2.py          # Standalone agent orchestrator (Strands)
├── emr_serverless_estimator.py      # Serverless cost estimation from event logs
├── glue_cost_estimator.py           # AWS Glue cost estimation (Standard + Flex)
├── report_generator.py              # Deterministic HTML report generator
├── scoring_config.yaml              # Config-driven scoring weights
└── region_mapping.yaml              # AWS region mappings
evals/
└── test_evals.py                    # 4-layer eval framework (36 tests)
curated_pdfs/                        # Expert knowledge base (deployment guides, best practices)
templates/                           # Report templates (HTML + Markdown)
orioniq_cli.py                       # Standalone CLI (no LLM required)
SKILL.md                             # Agent workflow context (loaded by Kiro CLI)
config.yaml                          # Default configuration
mcp_configuration_examples.md        # Advanced MCP configuration guide
```

## Cost Estimation Methodology

OrionIQ estimates costs differently for each deployment option:

| Option | How cost is estimated |
|--------|---------------------|
| **EMR on EC2 (On-Demand)** | Per-instance billing: `instance_type_rate × actual_runtime_hours` for each instance individually. Rates fetched from AWS Pricing API for the actual instance type (e.g., m5.xlarge at $0.192/hr + $0.048/hr EMR uplift). Handles mixed instance types and autoscaling. |
| **EMR on EC2 (Spot)** | Same as above but uses live spot prices from the EC2 API (cheapest AZ). Master node stays on-demand, core/task nodes on spot. EMR uplift applies to all instances regardless of spot/on-demand. |
| **EMR on EKS (On-Demand)** | EC2 instance cost (no EMR EC2 uplift) + EKS cluster fee ($0.10/hr) + EMR on EKS per-vCPU/GB uplift. EKS uses a different uplift model than EC2 — per compute used, not per instance. |
| **EMR on EKS (Spot)** | Same as EKS On-Demand but with spot instance pricing for EC2 compute. |
| **EMR Serverless** | Uses actual Spark executor metrics: `executors × cores × job_hours × vCPU_rate + executors × memory_gb × job_hours × GB_rate`. Precise when real Spark data is available from the History Server. |
| **AWS Glue (Standard)** | Maps Spark executors to Glue workers (1 worker = 1 executor). Picks smallest worker type that fits (G.1X through G.16X, or R-series for memory-intensive). Cost = `total_DPUs × $0.44/DPU-hr × job_hours`. R-series uses M-DPUs at $0.52/hr. |
| **AWS Glue (Flex)** | Same as Standard but at $0.29/DPU-hr (34% discount). Only available for G.1X and G.2X worker types. |

**Billing model:** EMR bills per-second with a 1-minute minimum. OrionIQ uses actual instance runtimes and per-instance-type rates from the AWS Pricing API.

For precise future cost projections, use the [AWS Pricing Calculator](https://calculator.aws/).

## Considerations

- **Cluster-based analysis is fully deterministic** — Spark data is fetched directly via the EMR Persistent App UI, bypassing the agent. Same input always produces same output.
- **App-ID-only analysis** (no cluster ID) routes Spark data through the agent via the Spark History Server MCP. The tool validates executor data format and rejects incorrect data, but provide a cluster ID when possible for the most accurate results.
- **Spot prices are live snapshots** — fetched from the EC2 API at analysis time. Actual costs may vary as spot prices fluctuate.
- **Glue pricing assumes fixed worker count** (no autoscaling). With Glue autoscaling enabled, actual cost may be lower for jobs with variable parallelism.
- **Glue Flex pricing** (34% discount) is only available for G.1X and G.2X worker types. Larger workers (G.4X+) and memory-optimized (R-series) do not qualify.
- **Knowledge base** (curated blogs and PDFs) is loaded at MCP server startup. Content is static — update the sources in `config.yaml` to refresh.

## Disclaimer

OrionIQ provides **cost estimates** based on real-time AWS pricing and actual workload metrics. These are approximations — actual costs may differ due to factors including but not limited to: autoscaling behavior, spot instance interruptions and price fluctuations, data transfer costs, S3 storage and request charges, dynamic allocation changes during job execution, and AWS pricing updates. OrionIQ does not account for Reserved Instance or Savings Plan discounts. Always validate estimates against your actual AWS billing before making migration decisions. For official cost projections, use the [AWS Pricing Calculator](https://calculator.aws/).

## Required IAM Permissions

OrionIQ calls the following AWS APIs via boto3. Minimum IAM policy:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "elasticmapreduce:DescribeCluster",
        "elasticmapreduce:ListInstances",
        "elasticmapreduce:GetManagedScalingPolicy",
        "elasticmapreduce:CreatePersistentAppUI",
        "elasticmapreduce:GetPersistentAppUIPresignedURL",
        "ec2:DescribeSpotPriceHistory",
        "pricing:GetProducts",
        "glue:GetJobRuns"
      ],
      "Resource": "*"
    }
  ]
}
```

**Notes:**
- `elasticmapreduce:CreatePersistentAppUI` is a write action — it creates a persistent Spark History Server UI for the cluster if one doesn't exist. Required for fetching Spark executor data from terminated clusters. If your environment restricts write actions, the tool will fall back to default Spark metrics.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## License

See [LICENSE](LICENSE).

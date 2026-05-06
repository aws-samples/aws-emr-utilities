# Building an MCP Server for the EMR Serverless Config Advisor

This document walks through the end-to-end process of creating a Model Context Protocol (MCP) server that lets Kiro CLI users ask natural language questions like *"share recommendations for this job in cost-optimized mode"* and get Spark configuration recommendations back.

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Prerequisites](#2-prerequisites)
3. [Step 1 — Set Up the Remote EC2 Instance](#step-1--set-up-the-remote-ec2-instance)
4. [Step 2 — Sync Pipeline Scripts to EC2](#step-2--sync-pipeline-scripts-to-ec2)
5. [Step 3 — Install the MCP Python SDK](#step-3--install-the-mcp-python-sdk)
6. [Step 4 — Create the MCP Server](#step-4--create-the-mcp-server)
7. [Step 5 — Register the MCP Server with Kiro CLI](#step-5--register-the-mcp-server-with-kiro-cli)
8. [Step 6 — Test the MCP Server](#step-6--test-the-mcp-server)
9. [Step 7 — Handle Edge Cases (NaN Bug Fix)](#step-7--handle-edge-cases-nan-bug-fix)
10. [How It Works End-to-End](#how-it-works-end-to-end)
11. [Troubleshooting](#troubleshooting)

---

## 1. Architecture Overview

### What is MCP?

The [Model Context Protocol (MCP)](https://modelcontextprotocol.io) is an open protocol that standardizes how AI applications provide context and tools to LLMs. Think of it as a USB-C port for AI — a standardized way to connect tools and data sources to any LLM-powered application.

MCP defines three primitives:
- **Tools** (model-controlled) — Functions the LLM can invoke to take actions (this is what we use)
- **Resources** (application-controlled) — Data the client application can load into context
- **Prompts** (user-controlled) — Reusable templates invoked by user choice

We built our server using the [official MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) (22k+ stars, MIT license), specifically the `FastMCP` high-level API that handles all protocol details.

### How Kiro CLI Connects to MCP Servers

Kiro CLI supports MCP servers via **stdio transport**: it launches your server as a child process and communicates over stdin/stdout using JSON-RPC. The flow:

1. Kiro reads `~/.kiro/settings/mcp.json` at startup
2. For each configured server, it spawns the process (`python3 spark_advisor_mcp.py`)
3. Kiro sends an `initialize` handshake, the server responds with its capabilities and tool list
4. When the user asks a question that maps to a tool, Kiro sends a `tools/call` request
5. The server executes the tool and returns the result over stdout
6. Kiro presents the result conversationally

No HTTP server, no ports, no networking between Kiro and the MCP server — it's all in-process via stdio.

### System Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│  User's Local Machine                                                │
│                                                                      │
│  ┌──────────┐     stdio      ┌──────────────────────┐               │
│  │ Kiro CLI  │◄──────────────►│ spark_advisor_mcp.py │               │
│  │ (chat)    │   (MCP proto)  │ (MCP Server)         │               │
│  └──────────┘                 └──────────┬───────────┘               │
│                                          │ SSH                       │
└──────────────────────────────────────────┼───────────────────────────┘
                                           │
                                           ▼
┌──────────────────────────────────────────────────────────────────────┐
│  EC2 Instance (747 GB RAM)                                           │
│                                                                      │
│  ┌────────────────────┐    ┌──────────────────┐    ┌──────────────┐ │
│  │ pipeline_wrapper.py │───►│ spark_processor  │───►│ emr_         │ │
│  │ (orchestrator)      │    │ (Stage 1:        │    │ recommender  │ │
│  │                     │    │  extract metrics)│    │ (Stage 2:    │ │
│  │                     │    │                  │    │  generate    │ │
│  │                     │    │                  │    │  configs)    │ │
│  └────────────────────┘    └──────────────────┘    └──────────────┘ │
│         │                          │                       │         │
│         ▼                          ▼                       ▼         │
│  ┌─────────────────────────────────────────────────────────────────┐ │
│  │                         Amazon S3                               │ │
│  │  Input: s3://bucket/event-logs/                                 │ │
│  │  Staging: s3://bucket/mcp-staging/{run_id}/                     │ │
│  │  Output: /tmp/recs_{run_id}_cost.json, _perf.json               │ │
│  └─────────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────────┘
```

The MCP server runs locally as a lightweight process. It SSHs into a high-memory EC2 instance to run the heavy Spark event log analysis pipeline. Results are read back via SSH and returned to Kiro as JSON.

**Why SSH to EC2?** The pipeline needs 97+ GB of RAM to process 10 applications (1,686 event log files). It can't run locally. We initially tried AWS Batch with Spot instances, but provisioning delays made the interactive experience poor. A persistent EC2 instance gives instant execution.

---

## 2. Prerequisites

| Requirement | Details |
|---|---|
| Python 3.10+ | On local machine |
| Kiro CLI | Installed and working |
| EC2 instance | High-memory instance with SSH access (we used a 747 GB RAM instance) |
| SSH key | PEM file for EC2 access (e.g., `~/suthan-bda.pem`) |
| AWS credentials | On the EC2 instance (for S3 access) |
| Pipeline scripts | `pipeline_wrapper.py`, `spark_processor.py`, `emr_recommender.py` |

---

## Step 1 — Set Up the Remote EC2 Instance

Ensure the EC2 instance is running and accessible via SSH:

```bash
# Test SSH connectivity
ssh -i ~/suthan-bda.pem hadoop@ec2-3-91-248-12.compute-1.amazonaws.com 'echo "Connected"'
```

Verify Python 3 and required packages are installed on EC2:

```bash
ssh -i ~/suthan-bda.pem hadoop@ec2-3-91-248-12.compute-1.amazonaws.com \
  'python3 --version && pip3 list 2>/dev/null | grep -E "boto3|pandas"'
```

Expected output:
```
Python 3.x.x
boto3          1.x.x
pandas         2.x.x
```

---

## Step 2 — Sync Pipeline Scripts to EC2

Copy the three pipeline scripts from your local repo to the EC2 instance's home directory:

```bash
cd ~/Documents/aws-emr-utilities/utilities/EMR-Serverless-Config-Advisor

scp -i ~/suthan-bda.pem \
  pipeline_wrapper.py \
  spark_processor.py \
  emr_recommender.py \
  hadoop@ec2-3-91-248-12.compute-1.amazonaws.com:~/
```

Verify they landed:

```bash
ssh -i ~/suthan-bda.pem hadoop@ec2-3-91-248-12.compute-1.amazonaws.com \
  'ls -lh pipeline_wrapper.py spark_processor.py emr_recommender.py'
```

---

## Step 3 — Install the MCP Python SDK

We used the official [Model Context Protocol Python SDK](https://github.com/modelcontextprotocol/python-sdk) (v1.x, MIT licensed) to build this server. The SDK provides `FastMCP`, a high-level Pythonic framework that handles all protocol compliance, message routing, and transport — so you only write your tool logic.

### Why FastMCP?

The raw MCP protocol requires implementing JSON-RPC message handling, capability negotiation, and transport management. FastMCP (originally a standalone project, now integrated into the official SDK) abstracts all of that. You just decorate Python functions with `@mcp.tool()` and the framework handles:

- Tool discovery (clients can list available tools and their schemas)
- Argument validation (from Python type hints and docstrings)
- Transport (stdio by default — the client launches your server as a subprocess and communicates over stdin/stdout)
- Protocol compliance (MCP spec handshake, capability advertisement, etc.)

### Installation

On your **local machine**, install the MCP SDK:

```bash
pip3 install mcp boto3
```

Or with `uv` (the SDK's recommended package manager):

```bash
uv add "mcp[cli]"
```

This provides the `mcp.server.fastmcp` module used to build the server.

### Key SDK Concepts Used

From the [SDK quickstart](https://github.com/modelcontextprotocol/python-sdk#quickstart):

| Concept | What it does | How we used it |
|---|---|---|
| `FastMCP(name)` | Creates a server instance with protocol handling | `mcp = FastMCP("spark-config-advisor")` |
| `@mcp.tool()` | Registers a function as an MCP tool | Decorated `analyze_spark_logs` and `list_event_log_prefixes` |
| `mcp.run()` | Starts the server (stdio transport by default) | Called in `if __name__ == "__main__"` |
| Type hints + docstrings | Auto-generates tool schemas for clients | Args like `input_path: str`, `mode: str = "cost-optimized"` become the tool's input schema |
| Return `str` | Tool output sent back to the client | We return `json.dumps(...)` strings |

The SDK's [tool documentation](https://github.com/modelcontextprotocol/python-sdk#tools) shows the pattern we followed — define a function, add type hints, write a docstring, and decorate with `@mcp.tool()`. The framework generates the JSON schema that clients use to discover and invoke the tool.

### Minimal MCP Server Pattern (from the SDK)

The official quickstart pattern we based our server on:

```python
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("Demo")

@mcp.tool()
def my_tool(arg1: str, arg2: int = 10) -> str:
    """Description becomes the tool's help text for the LLM."""
    return json.dumps({"result": "..."})

if __name__ == "__main__":
    mcp.run()
```

That's the entire boilerplate. Everything else is your business logic. We added SSH-based remote execution on top of this pattern.

References:
- [MCP Python SDK (GitHub)](https://github.com/modelcontextprotocol/python-sdk) — Official SDK, v1.26.0, MIT license
- [MCP Specification](https://modelcontextprotocol.io/specification/latest) — Protocol spec
- [FastMCP on PyPI](https://pypi.org/project/mcp/) — `pip install mcp`

---

## Step 4 — Create the MCP Server

Create the directory and server file:

```bash
mkdir -p mcp-server/
```

Create `mcp-server/spark_advisor_mcp.py` with the following content:

```python
#!/usr/bin/env python3
"""
Spark Config Advisor MCP Server
Runs analysis on a remote EC2 instance via SSH and returns recommendations.
"""

import json
import subprocess
import time
from mcp.server.fastmcp import FastMCP

EC2_HOST = "hadoop@ec2-3-91-248-12.compute-1.amazonaws.com"
SSH_KEY = "~/suthan-bda.pem"
SSH_CMD = f"ssh -i {SSH_KEY} -o StrictHostKeyChecking=no -o ConnectTimeout=10 {EC2_HOST}"

mcp = FastMCP("spark-config-advisor")


def _ssh(cmd, timeout=900):
    """Run command on EC2 via SSH."""
    result = subprocess.run(
        f'{SSH_CMD} \'{cmd}\'',
        shell=True, capture_output=True, text=True, timeout=timeout
    )
    return result.stdout, result.stderr, result.returncode
```

### Tool 1: `analyze_spark_logs`

This is the main tool. It:
1. Parses the S3 input path
2. Generates a unique `run_id` (Unix timestamp) for isolation
3. Builds the `pipeline_wrapper.py` command with all arguments
4. Executes it on EC2 via SSH
5. Reads the output JSON file from EC2
6. Returns structured results to Kiro

```python
@mcp.tool()
def analyze_spark_logs(
    input_path: str,
    mode: str = "cost-optimized",
    limit: int = 100,
) -> str:
    """Analyze Spark event logs from S3 and generate EMR Serverless
    configuration recommendations.

    Args:
        input_path: S3 path to event logs (e.g. s3://bucket/prefix/)
        mode: 'cost-optimized' or 'performance-optimized'
        limit: Max applications to analyze (default 100)

    Returns:
        JSON with recommendations per application.
    """
    if not input_path.startswith("s3://"):
        return json.dumps({"error": "input_path must be an S3 path"})

    parts = input_path.replace("s3://", "").split("/", 1)
    input_bucket = parts[0]
    input_prefix = parts[1] if len(parts) > 1 else ""

    run_id = str(int(time.time()))
    staging_prefix = f"mcp-staging/{run_id}/"
    output_file = f"/tmp/recs_{run_id}.json"

    # Build pipeline command
    pipeline_cmd = (
        f"cd ~ && python3 pipeline_wrapper.py"
        f" --input-bucket {input_bucket}"
        f" --input-prefix {input_prefix}"
        f" --staging-prefix {staging_prefix}"
        f" --output {output_file}"
        f" --limit {limit}"
    )
    if mode == "cost-optimized":
        pipeline_cmd += " --cost-optimized"
    elif mode == "performance-optimized":
        pipeline_cmd += " --performance-optimized"

    # Run pipeline on EC2 (can take 5-10 minutes)
    stdout, stderr, rc = _ssh(pipeline_cmd)

    if rc != 0:
        return json.dumps({
            "error": f"Pipeline failed (exit {rc})",
            "stderr": stderr[-2000:],
            "stdout": stdout[-2000:]
        })

    # Read results from EC2
    cost_file = output_file.replace(".json", "_cost.json")
    perf_file = output_file.replace(".json", "_perf.json")
    target = cost_file if mode == "cost-optimized" else perf_file

    out, err, rc = _ssh(f"cat {target}")
    if rc != 0:
        out, err, rc = _ssh(f"cat {cost_file} 2>/dev/null || cat {perf_file}")

    if rc != 0:
        return json.dumps({"error": "Could not read results", "stderr": err})

    try:
        results = json.loads(out)
        return json.dumps({
            "mode": mode,
            "application_count": len(results),
            "recommendations": results,
        }, indent=2)
    except json.JSONDecodeError:
        return json.dumps({"error": "Invalid JSON in results", "raw": out[:2000]})
```

### Tool 2: `list_event_log_prefixes`

A helper tool that lets Kiro browse available event logs in S3:

```python
@mcp.tool()
def list_event_log_prefixes(bucket: str = "suthan-event-logs", prefix: str = "") -> str:
    """List available Spark event log application prefixes in S3.

    Args:
        bucket: S3 bucket name
        prefix: S3 prefix to search under

    Returns:
        List of application prefixes found.
    """
    out, _, rc = _ssh(f"aws s3 ls s3://{bucket}/{prefix} --region us-east-1")
    if rc != 0:
        return json.dumps({"error": "Failed to list S3"})
    prefixes = [
        line.strip().split()[-1]
        for line in out.strip().split("\n")
        if line.strip().startswith("PRE")
    ]
    return json.dumps(prefixes, indent=2)
```

### Server Entry Point

```python
if __name__ == "__main__":
    mcp.run()
```

The `mcp.run()` call starts the server using **stdio transport** — Kiro CLI launches the process and communicates with it over stdin/stdout using the MCP protocol.

---

## Step 5 — Register the MCP Server with Kiro CLI

Create or edit `~/.kiro/settings/mcp.json`:

```json
{
  "mcpServers": {
    "spark-config-advisor": {
      "command": "python3",
      "args": [
        "/Users/suthan/Documents/aws-emr-utilities/utilities/EMR-Serverless-Config-Advisor/mcp-server/spark_advisor_mcp.py"
      ],
      "env": {
        "AWS_DEFAULT_REGION": "us-east-1"
      }
    }
  }
}
```

Key points:
- The config file location is `~/.kiro/settings/mcp.json` (not `~/.kiro/mcp-servers.json`)
- `command` + `args` tell Kiro how to launch the MCP server process
- `env` passes environment variables to the server process
- The server name `spark-config-advisor` is what appears in Kiro's tool list

---

## Step 6 — Test the MCP Server

### 6a. Verify Kiro loads the server

Start a new Kiro CLI session. You should see the MCP server tools loaded in the startup output. The tools `analyze_spark_logs` and `list_event_log_prefixes` should be available.

### 6b. Test with natural language

In the Kiro CLI chat, type:

```
Can you analyze the spark event logs at s3://suthan-event-logs/sample-10-apps/ in cost-optimized mode?
```

Kiro will:
1. Recognize this maps to the `analyze_spark_logs` tool
2. Call it with `input_path="s3://suthan-event-logs/sample-10-apps/"` and `mode="cost-optimized"`
3. The MCP server SSHs into EC2 and runs the pipeline
4. Stage 1 extracts metrics from all event logs (~5.5 minutes for 10 apps)
5. Stage 2 generates recommendations
6. Results are returned as JSON and Kiro presents them conversationally

### 6c. Test the listing tool

```
What event log prefixes are available in the suthan-event-logs bucket?
```

---

## Step 7 — Handle Edge Cases (NaN Bug Fix)

During testing, we discovered that one application (`00g0d965vta5qg0b`) had a missing `SparkListenerApplicationEnd` event, causing `application_end_time`, `total_run_duration_minutes`, and `total_run_duration_hours` to be `null`. This propagated as `NaN` in pandas and crashed Stage 2 with:

```
ValueError: cannot convert float NaN to integer
```

**Fix applied in `emr_recommender.py`:**

1. Added `df.fillna(0)` after creating the DataFrame to sanitize all null values:

```python
df = pd.DataFrame(flattened)

# Sanitize NaN values - replace with 0 for numeric columns
df = df.fillna(0)
```

2. Added NaN guards in `_get_timeout_configs`:

```python
def _get_timeout_configs(input_gb: float, duration_hours: float) -> Dict[str, str]:
    import math
    if math.isnan(input_gb): input_gb = 0
    if math.isnan(duration_hours): duration_hours = 0
    ...
```

After fixing, re-sync the updated file to EC2:

```bash
scp -i ~/suthan-bda.pem emr_recommender.py hadoop@ec2-3-91-248-12.compute-1.amazonaws.com:~/
```

---

## How It Works End-to-End

Here's the complete flow when a user asks Kiro for recommendations:

```
1. User: "share recommendations for s3://bucket/logs/ in cost-optimized mode"
         │
2. Kiro CLI recognizes intent → calls MCP tool `analyze_spark_logs`
         │
3. MCP server (local) receives tool call
         │
4. MCP server SSHs into EC2:
   ssh -i key.pem hadoop@ec2-host 'cd ~ && python3 pipeline_wrapper.py \
     --input-bucket bucket --input-prefix logs/ \
     --staging-prefix mcp-staging/1773047813/ \
     --output /tmp/recs_1773047813.json --limit 100 --cost-optimized'
         │
5. EC2 runs Stage 1 (spark_processor.py):
   - Downloads event logs from S3
   - Extracts metrics (IO, CPU, memory, shuffle, spill)
   - Writes intermediate JSON to S3 staging
         │
6. EC2 runs Stage 2 (emr_recommender.py):
   - Reads intermediate JSON from S3 staging
   - Generates dual-mode recommendations (cost + performance)
   - Writes /tmp/recs_1773047813_cost.json and _perf.json
         │
7. MCP server reads result file via SSH: cat /tmp/recs_1773047813_cost.json
         │
8. MCP server returns JSON to Kiro CLI
         │
9. Kiro presents recommendations conversationally to the user
```

---

## Troubleshooting

| Issue | Cause | Fix |
|---|---|---|
| MCP server not loading in Kiro | Wrong config path | Must be `~/.kiro/settings/mcp.json` |
| SSH connection timeout | EC2 instance stopped | Start the instance, verify security group allows SSH |
| Pipeline fails with `ModuleNotFoundError` | Missing deps on EC2 | `pip3 install boto3 pandas` on EC2 |
| `ValueError: cannot convert float NaN to integer` | Missing `SparkListenerApplicationEnd` event | Apply the NaN fix from Step 7 |
| Pipeline takes too long | Large dataset | Use `--limit` to cap number of apps, or `--skip-extraction` if staging data exists |
| Results file not found | Stage 2 failed silently | Check stderr in the MCP response, run `emr_recommender.py` manually on EC2 |
| AWS credentials error on EC2 | No IAM role or expired creds | Attach IAM role to EC2 instance or configure `~/.aws/credentials` |

---

## File Reference

| File | Location | Purpose |
|---|---|---|
| `spark_advisor_mcp.py` | `mcp-server/` | MCP server (runs locally, SSHs to EC2) |
| `pipeline_wrapper.py` | EC2 `~/` | Two-stage pipeline orchestrator |
| `spark_processor.py` | EC2 `~/` | Stage 1: extract metrics from Spark event logs |
| `emr_recommender.py` | EC2 `~/` | Stage 2: generate configuration recommendations |
| `mcp.json` | `~/.kiro/settings/` | Kiro CLI MCP server registration |

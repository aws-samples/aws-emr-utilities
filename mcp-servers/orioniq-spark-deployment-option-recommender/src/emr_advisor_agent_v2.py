#!/usr/bin/env python3
"""
OrionIQ EMR Deployment Advisor - v2 (Single Agent)

Refactored from 7-agent sequential pipeline to a single well-tooled agent.
Key improvements:
- Single agent with all tools — lets the LLM reason about workflow
- Structured context via file-based findings (no raw string forwarding)
- No prescriptive tool calls in prompts — agent decides what to call
- Clean separation: MCP servers provide tools, agent provides reasoning
"""

import logging
import os
import sys
import yaml
from datetime import datetime
from contextlib import ExitStack
from strands import Agent
from strands.models import BedrockModel
from strands.tools.mcp import MCPClient
from mcp import stdio_client, StdioServerParameters
from strands_tools import file_write, file_read

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.WARNING, format="%(message)s", handlers=[logging.StreamHandler(sys.stdout)])
logging.getLogger("strands").setLevel(logging.WARNING)
logging.getLogger("mcp").setLevel(logging.WARNING)
logging.getLogger("botocore").setLevel(logging.WARNING)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def load_config(path="config.yaml"):
    try:
        with open(path) as f:
            return yaml.safe_load(f) or {}
    except (FileNotFoundError, yaml.YAMLError):
        return {}

CONFIG = load_config()

def env(key, default=""):
    """Get from env first, then config, then default."""
    return os.environ.get(key, default)

# ---------------------------------------------------------------------------
# MCP Client Factories
# ---------------------------------------------------------------------------
def _mcp_env(**extra):
    base = {"FASTMCP_LOG_LEVEL": "WARNING"}
    base.update(extra)
    return base

def make_emr_advisor_client():
    region = env("AWS_REGION", CONFIG.get("aws", {}).get("region", "us-west-2"))
    knowledge = CONFIG.get("knowledge_sources", {})
    import json
    mcp_env = _mcp_env(
        AWS_REGION=region,
        EMR_CLUSTER_ID=env("EMR_CLUSTER_ID", CONFIG.get("aws", {}).get("emr_cluster_id", "")),
        EMR_CLUSTER_ARN=env("EMR_CLUSTER_ARN", CONFIG.get("aws", {}).get("emr_cluster_arn", "")),
        LLM_MODEL_ID=CONFIG.get("llm", {}).get("model_id", "anthropic.claude-3-5-sonnet-20241022-v2:0"),
        LOG_LEVEL="WARNING",
        SEMANTIC_MODEL_NAME=CONFIG.get("semantic_search", {}).get("model_name", "all-MiniLM-L6-v2"),
        SIMILARITY_THRESHOLD=str(CONFIG.get("semantic_search", {}).get("similarity_threshold", 0.1)),
    )
    if knowledge.get("blogs"):
        mcp_env["KNOWLEDGE_BLOGS_JSON"] = json.dumps(knowledge["blogs"])
    if knowledge.get("pdfs"):
        mcp_env["KNOWLEDGE_PDFS_JSON"] = json.dumps(knowledge["pdfs"])

    return MCPClient(lambda: stdio_client(StdioServerParameters(
        command="uv", args=["run", "src/combined_emr_advisor_mcp.py"], env=mcp_env
    )))

def make_aws_dp_client():
    region = env("AWS_REGION", CONFIG.get("aws", {}).get("region", "us-west-2"))
    return MCPClient(lambda: stdio_client(StdioServerParameters(
        command="uvx",
        args=["awslabs.aws-dataprocessing-mcp-server@latest", "--allow-write"],
        env=_mcp_env(AWS_REGION=region),
    )))

def make_pricing_client():
    return MCPClient(lambda: stdio_client(StdioServerParameters(
        command="uvx",
        args=["awslabs.aws-pricing-mcp-server@latest"],
        env=_mcp_env(),
    )))

def make_spark_history_client():
    spark_cfg = CONFIG.get("spark_history_mcp", {})
    spark_path = env("SPARK_HISTORY_MCP_PATH", spark_cfg.get("path", ""))
    if not spark_path or not os.path.exists(spark_path):
        return None
    cluster_arn = env("EMR_CLUSTER_ARN", CONFIG.get("aws", {}).get("emr_cluster_arn", ""))
    return MCPClient(
        lambda: stdio_client(StdioServerParameters(
            command="uv",
            args=["--directory", spark_path, "run", "src/spark_history_mcp/core/main.py"],
            env=_mcp_env(
                SHS_SERVERS_EMR_EMRCLUSTERARN=cluster_arn,
                SHS_MCP_TRANSPORT="stdio",
                SHS_SERVERS_EMR_DEFAULT="true",
            ),
        )),
        startup_timeout=spark_cfg.get("startup_timeout", 120),
    )

# ---------------------------------------------------------------------------
# System Prompt
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """You are OrionIQ, an expert AWS EMR deployment advisor.

You have access to tools from multiple MCP servers:
- EMR Advisor tools: workload context, Spark analysis, cluster analysis, pricing + recommendation
- AWS Data Processing tools: live EMR cluster inspection (describe clusters, instances, steps)
- AWS Pricing tools: real-time AWS pricing lookups
- Spark History Server tools: Spark application metrics and performance data
- File tools: read/write analysis reports

PHASE 1 — ANALYSIS PLAN (before calling any tools):

Present an analysis plan to the user and ask them to confirm. The plan should include:

1. What data you will analyze (cluster IDs, app IDs, event logs — based on what they provided)
2. Ask their priorities — present these options and ask them to rank or pick top 2:
   - 💰 Lowest cost (minimize spend)
   - 🔧 Simplest operations (minimize management overhead)
   - ⚡ Best performance (minimize latency/runtime)
   - 🛡️ Highest reliability (multi-AZ, fault tolerance)
   - 📈 Best scalability (handle growth/spikes)
3. Any constraints (e.g., "no Kubernetes", "must use spot", "needs private subnet")
4. Expected output format (summary vs detailed report)

Wait for user confirmation before proceeding to Phase 2.

PHASE 2 — DATA COLLECTION & RECOMMENDATION (4 steps):

1. collect_workload_context() — Gather user constraints and knowledge base insights. NO recommendation here.
2. analyze_spark_job_config() — Extract Spark config from event logs or History Server
3. analyze_cluster_patterns() — Analyze real cluster data (utilization, idle time, spot usage)
4. get_emr_pricing() — Calculate actual costs AND produce the final evidence-based recommendation

The user's priorities from Phase 1 adjust the scoring weights in Step 4:
- If user picked "lowest cost" → cost weight increases to 60%
- If user picked "simplest operations" → operational fit weight increases
- If user picked "best performance" → workload fit weight increases
- Default weights: cost 40%, workload fit 30%, operational fit 20%, constraints 10%

CRITICAL RULES:
- Do NOT skip Phase 1 — always present the plan and ask for confirmation
- Do NOT recommend a deployment option before Step 4 completes
- The recommendation must cite EVIDENCE from real data, not generic advice
- If a tool fails, note it and continue — partial data is better than no data
- Write the final report to the output directory with evidence for each recommendation
"""

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    print("\n🚀 OrionIQ EMR Deployment Advisor v2 (Single Agent)\n")

    os.environ.setdefault("BYPASS_TOOL_CONSENT", "true")

    # Clear stale AWS session tokens
    for var in ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"]:
        os.environ.pop(var, None)

    # Build MCP clients
    clients = {
        "emr_advisor": make_emr_advisor_client(),
        "aws_dp": make_aws_dp_client(),
        "pricing": make_pricing_client(),
    }
    spark_client = make_spark_history_client()
    if spark_client:
        clients["spark_history"] = spark_client
    else:
        print("⚠️  Spark History Server MCP not configured — Spark app analysis will be limited")

    # Model
    llm_cfg = CONFIG.get("llm", {})
    model = BedrockModel(
        model_id=llm_cfg.get("model_id", "anthropic.claude-3-5-sonnet-20241022-v2:0"),
        region_name=llm_cfg.get("region", "us-west-2"),
        temperature=llm_cfg.get("temperature", 0.1),
        max_tokens=llm_cfg.get("max_tokens", 4000),
    )

    # Open all MCP clients and run agent
    with ExitStack() as stack:
        for c in clients.values():
            stack.enter_context(c)

        # Gather all tools
        all_tools = [file_read, file_write]
        for name, client in clients.items():
            tools = client.list_tools_sync()
            print(f"  ✅ {name}: {len(tools)} tools")
            all_tools.extend(tools)

        print(f"\n  Total tools available: {len(all_tools)}\n")

        # Create single agent
        agent = Agent(
            model=model,
            system_prompt=SYSTEM_PROMPT,
            tools=all_tools,
        )

        # Prepare output dir
        os.makedirs("output", exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        # Conversation loop — supports Phase 1 (plan) → Phase 2 (execute)
        print("🔍 Describe your EMR analysis request (or 'quit' to exit):\n")
        while True:
            user_input = input("> ").strip()
            if not user_input or user_input.lower() in ("quit", "exit", "q"):
                break

            # Append report path hint on first message
            prompt = f"{user_input}\n\nIf writing a report, save to output/EMR_Analysis_Report_{timestamp}.md"

            try:
                result = agent(prompt)
            except Exception as e:
                print(f"\n❌ Error: {e}")
                continue

        print("\n✅ Session complete. Check output/ for any generated reports.")


if __name__ == "__main__":
    main()

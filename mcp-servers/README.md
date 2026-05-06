# MCP Servers for AWS EMR

[Model Context Protocol (MCP)](https://modelcontextprotocol.io) servers that connect AI agents to AWS EMR infrastructure for intelligent analysis and optimization.

## Available Servers

| Server | Description | Tools |
|---|---|---|
| [EMR Serverless Spark Advisor](emr-serverless-spark-advisor/) | Analyze Spark event logs, identify bottlenecks, and generate optimized EMR Serverless configurations | 12 |

## What is MCP?

MCP is an open protocol that standardizes how AI applications provide context to LLMs. These MCP servers enable AI agents (Kiro, Claude Desktop, Amazon Q CLI, LangGraph, etc.) to interact with your EMR infrastructure through natural language.

```
User: "Why is my Spark job slow?"

AI Agent ──► MCP Server ──► Analyzes event logs ──► "Stage 8 takes 45% of runtime,
                                                      203 executors were idle,
                                                      2 TB disk spill detected.
                                                      Recommend: increase memory to 108g,
                                                      reduce max executors to 59"
```

## Getting Started

Each server has its own README with setup instructions. See the individual server directories above.

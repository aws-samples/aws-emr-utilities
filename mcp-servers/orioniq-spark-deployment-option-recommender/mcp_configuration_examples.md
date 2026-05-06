# 🔧 EMR Advisor MCP Configuration Guide

This guide shows different ways to configure the EMR Advisor MCP server for external use.

## 📋 Configuration Methods (Priority Order)

1. **Environment Variables** (Highest Priority)
2. **Custom Config File**
3. **Default config.yaml**
4. **Built-in Defaults** (Lowest Priority)

## 🌍 Method 1: Environment Variables

### Basic Configuration
```bash
# AWS Configuration
export AWS_REGION="us-west-2"
export EMR_CLUSTER_ID="j-YOUR-CLUSTER-ID"
export EMR_CLUSTER_ARN="arn:aws:elasticmapreduce:us-west-2:123456789012:cluster/j-YOUR-CLUSTER-ID"

# LLM Configuration
export LLM_MODEL_ID="anthropic.claude-3-5-sonnet-20241022-v2:0"
export LLM_TEMPERATURE="0.1"
export LLM_MAX_TOKENS="4000"

# App Configuration
export LOG_LEVEL="INFO"
export MAX_RETRIES="3"

# Semantic Search Configuration
export SEMANTIC_MODEL_NAME="all-MiniLM-L6-v2"
export SIMILARITY_THRESHOLD="0.1"
```

### Advanced Configuration with Knowledge Sources
```bash
# Knowledge Sources (JSON format)
export KNOWLEDGE_BLOGS_JSON='[
  {
    "blog_summary": "EMR-EKS vs EMR-EC2 comparison",
    "url": "https://aws.amazon.com/blogs/big-data/run-fault-tolerant-and-cost-optimized-spark-clusters-using-amazon-emr-on-eks-and-amazon-ec2-spot-instances"
  },
  {
    "blog_summary": "Best practices for running Spark on EKS",
    "url": "https://aws.amazon.com/blogs/containers/best-practices-for-running-spark-on-amazon-eks"
  }
]'

export KNOWLEDGE_PDFS_JSON='[
  {
    "path": "/path/to/your/emr-deployment-guide.pdf",
    "summary": "Custom EMR deployment guide"
  }
]'

# Custom config file path
export EMR_ADVISOR_CONFIG_PATH="/path/to/your/custom-config.yaml"
```

## 📄 Method 2: Custom Config File

### Example: `my-emr-config.yaml`
```yaml
# Custom EMR Advisor Configuration
aws:
  region: "eu-west-1"
  emr_cluster_id: "j-MYCUSTOMERCLUSTER"
  emr_cluster_arn: "arn:aws:elasticmapreduce:eu-west-1:123456789012:cluster/j-MYCUSTOMERCLUSTER"

llm:
  model_id: "anthropic.claude-3-haiku-20240307-v1:0"  # Use cheaper model
  temperature: 0.2
  max_tokens: 2000

app:
  log_level: "DEBUG"
  max_retries: 5
  initial_retry_delay: 15

semantic_search:
  model_name: "sentence-transformers/all-mpnet-base-v2"  # Better model
  similarity_threshold: 0.2
  top_k_results: 10

knowledge_sources:
  blogs:
    - blog_summary: "Your company's EMR best practices"
      url: "https://your-company.com/emr-guide"
    - blog_summary: "Custom Spark optimization guide"
      url: "https://your-company.com/spark-optimization"
  
  pdfs:
    - path: "/company/docs/emr-standards.pdf"
      summary: "Company EMR deployment standards"
    - path: "/company/docs/cost-optimization.pdf"
      summary: "Cost optimization guidelines"

# Custom pricing (if different region)
emr_serverless_pricing:
  vcpu_per_hour: 0.055  # EU pricing
  gb_per_hour: 0.006
```

## 🐳 Method 3: Docker Environment

### Dockerfile Example
```dockerfile
FROM python:3.11-slim

# Install uv
RUN pip install uv

# Copy application
COPY . /app
WORKDIR /app

# Install dependencies
RUN uv sync

# Set default environment variables
ENV AWS_REGION=us-west-2
ENV LOG_LEVEL=INFO
ENV LLM_MODEL_ID=anthropic.claude-3-5-sonnet-20241022-v2:0
ENV SEMANTIC_MODEL_NAME=all-MiniLM-L6-v2

# Run MCP server
CMD ["uv", "run", "src/combined_emr_advisor_mcp.py"]
```

### Docker Compose Example
```yaml
version: '3.8'
services:
  emr-advisor-mcp:
    build: .
    environment:
      - AWS_REGION=us-west-2
      - EMR_CLUSTER_ID=j-YOUR-CLUSTER-ID
      - LLM_MODEL_ID=anthropic.claude-3-5-sonnet-20241022-v2:0
      - LOG_LEVEL=INFO
      - KNOWLEDGE_BLOGS_JSON=[{"blog_summary":"Custom blog","url":"https://example.com"}]
    volumes:
      - ./custom-config.yaml:/app/config.yaml
      - ./pdfs:/app/curated_pdfs
    stdin_open: true
    tty: true
```

## 🔌 Method 4: MCP Client Integration

### For Kiro/Claude Desktop
```json
{
  "mcpServers": {
    "emr-advisor": {
      "command": "uv",
      "args": [
        "run", 
        "--directory", "/path/to/emr-advisor",
        "src/combined_emr_advisor_mcp.py"
      ],
      "env": {
        "FASTMCP_LOG_LEVEL": "INFO",
        "AWS_REGION": "us-west-2",
        "EMR_CLUSTER_ID": "j-YOUR-CLUSTER-ID",
        "LLM_MODEL_ID": "anthropic.claude-3-5-sonnet-20241022-v2:0",
        "LOG_LEVEL": "INFO",
        "SEMANTIC_MODEL_NAME": "all-MiniLM-L6-v2",
        "EMR_ADVISOR_CONFIG_PATH": "/path/to/your/config.yaml"
      },
      "timeout": 120000,
      "disabled": false
    }
  }
}
```

### For Strands Agent Integration
```python
from mcp.client.streamable_http import streamablehttp_client
from mcp import stdio_client, StdioServerParameters
from strands.tools.mcp import MCPClient

# Method 1: Environment variables
emr_advisor_mcp_client = MCPClient(
    lambda: stdio_client(StdioServerParameters(
        command="uv",
        args=["run", "src/combined_emr_advisor_mcp.py"],
        env={
            "FASTMCP_LOG_LEVEL": "INFO",
            "AWS_REGION": "us-west-2",
            "EMR_CLUSTER_ID": "j-YOUR-CLUSTER-ID",
            "LLM_MODEL_ID": "anthropic.claude-3-5-sonnet-20241022-v2:0",
            "LOG_LEVEL": "DEBUG",
            "SEMANTIC_MODEL_NAME": "all-MiniLM-L6-v2",
            "SIMILARITY_THRESHOLD": "0.15"
        }
    ))
)

# Method 2: Custom config file
emr_advisor_mcp_client = MCPClient(
    lambda: stdio_client(StdioServerParameters(
        command="uv",
        args=["run", "src/combined_emr_advisor_mcp.py"],
        env={
            "FASTMCP_LOG_LEVEL": "INFO",
            "EMR_ADVISOR_CONFIG_PATH": "/path/to/my-custom-config.yaml"
        }
    ))
)
```

## 🎯 Method 5: Programmatic Configuration

### Python Integration Example
```python
import os
import json
from mcp.client.streamable_http import streamablehttp_client
from mcp import stdio_client, StdioServerParameters
from strands.tools.mcp import MCPClient

def create_emr_advisor_client(
    aws_region="us-west-2",
    emr_cluster_id=None,
    llm_model_id="anthropic.claude-3-5-sonnet-20241022-v2:0",
    log_level="INFO",
    custom_blogs=None,
    custom_pdfs=None,
    config_file_path=None
):
    """Create EMR Advisor MCP client with custom configuration"""
    
    env = {
        "FASTMCP_LOG_LEVEL": log_level,
        "AWS_REGION": aws_region,
        "LLM_MODEL_ID": llm_model_id,
        "LOG_LEVEL": log_level
    }
    
    if emr_cluster_id:
        env["EMR_CLUSTER_ID"] = emr_cluster_id
    
    if custom_blogs:
        env["KNOWLEDGE_BLOGS_JSON"] = json.dumps(custom_blogs)
    
    if custom_pdfs:
        env["KNOWLEDGE_PDFS_JSON"] = json.dumps(custom_pdfs)
    
    if config_file_path:
        env["EMR_ADVISOR_CONFIG_PATH"] = config_file_path
    
    return MCPClient(
        lambda: stdio_client(StdioServerParameters(
            command="uv",
            args=["run", "src/combined_emr_advisor_mcp.py"],
            env=env
        ))
    )

# Usage example
client = create_emr_advisor_client(
    aws_region="eu-west-1",
    emr_cluster_id="j-MYCUSTOMERCLUSTER",
    llm_model_id="anthropic.claude-3-haiku-20240307-v1:0",
    custom_blogs=[
        {
            "blog_summary": "Company EMR guide",
            "url": "https://company.com/emr-guide"
        }
    ]
)
```

## 🔍 Configuration Validation

### Test Your Configuration
```python
# Test script: test_mcp_config.py
import os
import json
import yaml

def validate_mcp_configuration():
    """Validate MCP configuration before starting"""
    
    print("🧪 Validating EMR Advisor MCP Configuration")
    print("=" * 50)
    
    # Check AWS configuration
    aws_region = os.environ.get('AWS_REGION', 'us-west-2')
    emr_cluster_id = os.environ.get('EMR_CLUSTER_ID')
    
    print(f"✅ AWS Region: {aws_region}")
    if emr_cluster_id:
        print(f"✅ EMR Cluster ID: {emr_cluster_id}")
    else:
        print("⚠️  EMR Cluster ID not set (will use default)")
    
    # Check LLM configuration
    llm_model = os.environ.get('LLM_MODEL_ID', 'anthropic.claude-3-5-sonnet-20241022-v2:0')
    print(f"✅ LLM Model: {llm_model}")
    
    # Check knowledge sources
    blogs_json = os.environ.get('KNOWLEDGE_BLOGS_JSON')
    if blogs_json:
        try:
            blogs = json.loads(blogs_json)
            print(f"✅ Custom blogs configured: {len(blogs)} entries")
        except json.JSONDecodeError:
            print("❌ Invalid KNOWLEDGE_BLOGS_JSON format")
    
    # Check config file
    config_path = os.environ.get('EMR_ADVISOR_CONFIG_PATH', 'config.yaml')
    if os.path.exists(config_path):
        print(f"✅ Config file found: {config_path}")
        try:
            with open(config_path, 'r') as f:
                config = yaml.safe_load(f)
            print(f"✅ Config file is valid YAML")
        except yaml.YAMLError as e:
            print(f"❌ Config file YAML error: {e}")
    else:
        print(f"📝 Config file not found: {config_path} (using defaults)")
    
    print("\n🚀 Configuration validation complete!")

if __name__ == "__main__":
    validate_mcp_configuration()
```

## 📚 Quick Start Examples

### Minimal Setup
```bash
# Set only essential parameters
export AWS_REGION="us-west-2"
export EMR_CLUSTER_ID="j-YOUR-CLUSTER-ID"

# Run MCP server
uv run src/combined_emr_advisor_mcp.py
```

### Production Setup
```bash
# Comprehensive production configuration
export AWS_REGION="us-west-2"
export EMR_CLUSTER_ID="j-PROD-CLUSTER-ID"
export EMR_CLUSTER_ARN="arn:aws:elasticmapreduce:us-west-2:123456789012:cluster/j-PROD-CLUSTER-ID"
export LLM_MODEL_ID="anthropic.claude-3-5-sonnet-20241022-v2:0"
export LOG_LEVEL="INFO"
export SEMANTIC_MODEL_NAME="sentence-transformers/all-mpnet-base-v2"
export SIMILARITY_THRESHOLD="0.2"
export EMR_ADVISOR_CONFIG_PATH="/etc/emr-advisor/production-config.yaml"

# Run with production settings
uv run src/combined_emr_advisor_mcp.py
```

This configuration system provides maximum flexibility for external users while maintaining backward compatibility with the existing `config.yaml` approach.
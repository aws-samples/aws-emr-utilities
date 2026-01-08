# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "strands-agents>=0.1.0",
#     "mcp-proxy-for-aws>=0.1.0",
#     "boto3>=1.34.0",
# ]
# ///
"""
Spark Workload Analyzer

This script analyzes Spark workloads on AWS Glue, EMR, and EMR Serverless
by calling MCP tools directly without using an LLM agent. The raw tool
outputs are returned for Claude Code to parse and present.

Usage:
    uv run troubleshoot_spark_workload.py --execution-id <id> --execution-type-id <type_id> \\
        --platform-type <glue|emr_serverless|emr_ec2> --profile <aws_profile> --region <aws_region>
"""

import argparse
import json
import logging
import sys
import uuid
from datetime import timedelta
from typing import Any, Dict, Literal

import boto3
from mcp_proxy_for_aws.client import aws_iam_streamablehttp_client
from strands.tools.mcp import MCPClient

# Configure logging to stderr (results go to stdout)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger(__name__)

MCP_ENDPOINT_TEMPLATE = (
    "https://sagemaker-unified-studio-mcp.{region}.api.aws/{server_name}/mcp"
)

PlatformType = Literal["emr_serverless", "glue", "emr_ec2"]


def _should_get_code_recommendations(analysis_result: dict) -> bool:
    """
    Check if the analysis result indicates we should get code recommendations.

    Args:
        analysis_result: Result from analyze_spark_workload tool

    Returns:
        True if code recommendations should be fetched
    """
    try:
        analysis_next_action = analysis_result.get("next_action", {})
        should_recommend = "spark_code_recommendation" in analysis_next_action
        logger.info(f"Should get code recommendations: {should_recommend}")
        return should_recommend
    except Exception as e:
        logger.error(f"Error checking if code recommendations needed: {e}")
        return False


def get_mcp_client_factory(server_name: str, session: boto3.Session, region: str):
    """Create an MCP client factory for the given server."""
    credentials = session.get_credentials()
    endpoint = MCP_ENDPOINT_TEMPLATE.format(region=region, server_name=server_name)

    return lambda: aws_iam_streamablehttp_client(
        endpoint=endpoint,
        aws_region=region,
        aws_service="sagemaker-unified-studio-mcp",
        credentials=credentials,
    )


def build_platform_params(
    platform_type: PlatformType,
    execution_id: str,
    execution_type_id: str,
) -> Dict[str, str]:
    """Build platform-specific parameters for MCP tools."""
    if platform_type == "glue":
        return {
            "job_name": execution_type_id,
            "job_run_id": execution_id,
        }
    elif platform_type == "emr_serverless":
        return {
            "application_id": execution_type_id,
            "job_run_id": execution_id,
        }
    elif platform_type == "emr_ec2":
        return {
            "cluster_id": execution_type_id,
            "step_id": execution_id,
        }
    else:
        raise ValueError(f"Unsupported platform type: {platform_type}")


def run_analysis(
    platform_type: PlatformType,
    execution_id: str,
    execution_type_id: str,
    profile: str,
    region: str,
) -> Dict[str, Any]:
    """
    Run Spark workload analysis using direct MCP tool calls.

    Args:
        platform_type: The platform type (glue, emr_serverless, emr_ec2)
        execution_id: The execution identifier
        execution_type_id: The resource identifier
        profile: AWS profile name
        region: AWS region

    Returns:
        Dictionary with analysis results and optional code recommendations
    """
    # Create boto3 session with the specified profile
    logger.info(f"Creating AWS session with profile: {profile}, region: {region}")
    session = boto3.Session(profile_name=profile, region_name=region)

    # Create MCP clients
    logger.info("Creating MCP clients...")
    spark_troubleshooting = MCPClient(
        get_mcp_client_factory("spark-troubleshooting", session, region)
    )
    spark_code_recommendation = MCPClient(
        get_mcp_client_factory("spark-code-recommendation", session, region)
    )

    # Build platform parameters
    platform_params = build_platform_params(
        platform_type, execution_id, execution_type_id
    )
    mcp_platform_type = platform_type.upper()  # emr_serverless -> EMR_SERVERLESS

    # Prepare tool parameters
    tool_params = {
        "platform_type": mcp_platform_type,
        "platform_params": platform_params,
    }

    logger.info(f"Calling analyze_spark_workload with params: {tool_params}")

    # Call analyze_spark_workload tool directly
    with spark_troubleshooting as client:
        response = client.call_tool_sync(
            str(uuid.uuid4()),
            "analyze_spark_workload",
            tool_params,
            timedelta(seconds=180),
        )
        if response["status"] != "success":
            raise Exception(f"analyze_spark_workload failed: {response}")
        analysis_result = json.loads(response["content"][0]["text"])

    logger.info(f"analyze_spark_workload completed")

    # Build output structure
    output = {
        "platform_type": platform_type,
        "platform_params": platform_params,
        "analysis_result": analysis_result,
        "code_recommendation": None,
    }

    # Check if we need code recommendations
    if _should_get_code_recommendations(analysis_result):
        logger.info("Getting code recommendations...")
        code_params = {
            "platform_type": mcp_platform_type,
            "platform_params": platform_params,
        }

        with spark_code_recommendation as client:
            response = client.call_tool_sync(
                str(uuid.uuid4()),
                "spark_code_recommendation",
                code_params,
                timedelta(seconds=180),
            )
            if response["status"] != "success":
                logger.error(f"spark_code_recommendation failed: {response}")
            else:
                code_result = json.loads(response["content"][0]["text"])
                output["code_recommendation"] = code_result
                logger.info("Code recommendations retrieved")

    return output


def main():
    parser = argparse.ArgumentParser(
        description="Analyze Spark workloads using direct MCP tool calls"
    )
    parser.add_argument(
        "--execution-id",
        required=True,
        help="Execution ID (Glue Job Run ID, EMR Step ID, or EMR Serverless Job Run ID)",
    )
    parser.add_argument(
        "--execution-type-id",
        required=True,
        help="Resource ID (Glue Job Name, EMR Cluster ID, or EMR Serverless Application ID)",
    )
    parser.add_argument(
        "--platform-type",
        required=True,
        choices=["glue", "emr_serverless", "emr_ec2"],
        help="Platform type",
    )
    parser.add_argument(
        "--profile",
        required=True,
        help="AWS profile name (Ask user which one)",
    )
    parser.add_argument(
        "--region",
        required=True,
        help="AWS region (Ask user which one')",
    )

    args = parser.parse_args()

    try:
        result = run_analysis(
            platform_type=args.platform_type,
            execution_id=args.execution_id,
            execution_type_id=args.execution_type_id,
            profile=args.profile,
            region=args.region,
        )
        # Output raw JSON for Claude Code to parse
        print(json.dumps(result, indent=2))
    except Exception as e:
        logger.error(f"Analysis failed: {e}", exc_info=True)
        # Output error as JSON for consistent parsing
        error_output = {
            "error": str(e),
            "platform_type": args.platform_type,
            "execution_id": args.execution_id,
            "execution_type_id": args.execution_type_id,
        }
        print(json.dumps(error_output, indent=2))
        sys.exit(1)


if __name__ == "__main__":
    main()

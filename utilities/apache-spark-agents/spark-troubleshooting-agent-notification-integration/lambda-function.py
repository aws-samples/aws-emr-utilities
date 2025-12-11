"""
AWS Lambda function for Apache Spark Troubleshooting using Strands MCP client.
Triggered by EventBridge, analyzes Spark workloads, and sends results to SNS.

Supports three platform types:
- EMR EC2: Triggered by "EMR Step Status Change" events
- EMR Serverless: Triggered by "EMR Serverless Job Run State Change" events
- Glue: Triggered by "Glue Job State Change" events
"""

import json
import os
import boto3
import uuid
import time
from datetime import timedelta
from typing import Dict, Any, Optional
import traceback
from strands.tools.mcp import MCPClient
from mcp_proxy_for_aws.client import aws_iam_streamablehttp_client
from botocore.credentials import Credentials


def determine_platform_type(event):
    """
    Determine the platform type from the EventBridge event.
    
    Args:
        event: EventBridge event
        
    Returns:
        Dictionary with platform_type, platform_params, and resource_id, or None if unsupported
    """
    source = event.get('source', '')
    detail_type = event.get('detail-type', '')
    detail = event.get('detail', {})
    
    # EMR EC2
    if source == 'aws.emr' and detail_type == 'EMR Step Status Change':
        cluster_id = detail.get('clusterId')
        step_id = detail.get('stepId')
        
        if not cluster_id or not step_id:
            print(f"ERROR: Missing required EMR parameters. clusterId: {cluster_id}, stepId: {step_id}")
            return None
        
        return {
            'platform_type': 'EMR_EC2',
            'platform_params': {
                'cluster_id': cluster_id,
                'step_id': step_id
            },
            'resource_id': f"{cluster_id}/{step_id}"
        }
    
    # EMR Serverless
    elif source == 'aws.emr-serverless' and detail_type == 'EMR Serverless Job Run State Change':
        application_id = detail.get('applicationId')
        job_run_id = detail.get('jobRunId')
        
        if not application_id or not job_run_id:
            print(f"ERROR: Missing required EMR Serverless parameters. applicationId: {application_id}, jobRunId: {job_run_id}")
            return None
        
        return {
            'platform_type': 'EMR_SERVERLESS',
            'platform_params': {
                'application_id': application_id,
                'job_run_id': job_run_id
            },
            'resource_id': f"{application_id}/{job_run_id}"
        }
    
    # Glue
    elif source == 'aws.glue' and detail_type == 'Glue Job State Change':
        job_name = detail.get('jobName')
        job_run_id = detail.get('jobRunId')
        
        if not job_name or not job_run_id:
            print(f"ERROR: Missing required Glue parameters. jobName: {job_name}, jobRunId: {job_run_id}")
            return None
        
        return {
            'platform_type': 'GLUE',
            'platform_params': {
                'job_name': job_name,
                'job_run_id': job_run_id
            },
            'resource_id': f"{job_name}/{job_run_id}"
        }
    
    # Unsupported event type
    else:
        print(f"Unsupported event - source: {source}, detail-type: {detail_type}")
        return None


def lambda_handler(event, context):
    """
    Lambda handler function triggered by EventBridge.
    
    Supports three platform types:
    - EMR EC2: EMR Step Status Change events
    - EMR Serverless: EMR Serverless Job Run State Change events
    - Glue: Glue Job State Change events
    """
    try:
        print(f"Received event: {json.dumps(event)}")
        
        # Determine platform type and extract parameters
        platform_info = determine_platform_type(event)
        
        if not platform_info:
            error_msg = "Unsupported compute type. Expected EMR, EMR Serverless, or Glue event."
            print(f"ERROR: {error_msg}")
            return {
                'statusCode': 400,
                'body': json.dumps({
                    'error': error_msg,
                    'source': event.get('source'),
                    'detail-type': event.get('detail-type')
                })
            }
        
        platform_type = platform_info['platform_type']
        platform_params = platform_info['platform_params']
        resource_id = platform_info['resource_id']
        
        print(f"Processing analysis for {platform_type}: {resource_id}")
        
        # Wait 1 minute to allow logs to be available for analysis (EMR EC2 only)
        if platform_type == 'EMR_EC2':
            print("⏳ Waiting 60 seconds for EMR logs to be ready...")
            time.sleep(60)
            print("✅ Wait complete, proceeding with analysis...")
        
        # Perform the Spark workload analysis
        analysis_result = analyze_spark_workload(platform_type, platform_params)
        
        # Check if we need to get code recommendations based on error category
        # Note: Code recommendations are not supported for EMR Serverless
        code_recommendation_result = None
        if should_get_code_recommendations(analysis_result, platform_type):
            print("🔧 Error category and platform type requires code recommendations, calling spark_code_recommendation tool...")
            code_recommendation_result = get_spark_code_recommendations(platform_type, platform_params)
        else:
            print("ℹ️ Code recommendations are not supported, skipping...")
        
        # Format the message for SNS
        sns_message = format_sns_message(platform_type, resource_id, analysis_result, code_recommendation_result)
        
        # Send to SNS
        sns_topic_arn = os.environ.get('SNS_TOPIC_ARN')
        if not sns_topic_arn:
            raise ValueError("SNS_TOPIC_ARN environment variable not set")
        
        send_to_sns(sns_topic_arn, sns_message, platform_type, resource_id)
        
        return {
            'statusCode': 200,
            'body': json.dumps({
                'message': 'Analysis completed successfully',
                'platformType': platform_type,
                'resourceId': resource_id,
                'analysisId': analysis_result.get('analysis_id', 'N/A')
            })
        }
        
    except Exception as e:
        error_msg = f"Lambda execution failed: {str(e)}"
        print(f"ERROR: {error_msg}")
        print(f"Traceback: {traceback.format_exc()}")
        
        # Send error notification to SNS if possible
        try:
            sns_topic_arn = os.environ.get('SNS_TOPIC_ARN')
            if sns_topic_arn:
                platform_info = determine_platform_type(event)
                resource_id = platform_info['resource_id'] if platform_info else 'Unknown'
                error_message = format_error_message(resource_id, error_msg)
                send_to_sns(sns_topic_arn, error_message, 'ERROR', 'LAMBDA_FAILURE')
        except Exception as sns_error:
            print(f"Failed to send error to SNS: {sns_error}")
        
        return {
            'statusCode': 500,
            'body': json.dumps({
                'error': error_msg
            })
        }


def should_get_code_recommendations(analysis_result: dict, platform_type: str) -> bool:
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
        print(f"Should get code recommendations: {should_recommend}")
        return should_recommend
    except Exception as e:
        print(f"Error checking if code recommendations needed: {e}")
        return False


def get_spark_code_recommendations(platform_type: str, platform_params: dict) -> dict:
    """
    Get Spark code recommendations using MCP client.
    
    Args:
        platform_type: Platform type (EMR_EC2, EMR_SERVERLESS, or GLUE)
        platform_params: Platform-specific parameters
        
    Returns:
        Code recommendation result dictionary
    """
    try:
        # Initialize MCP client provider
        client_provider = MCPClientProvider(
            region=os.environ.get('AWS_REGION')
        )
        
        # Get MCP client
        mcp_client_factory = client_provider.get_client_factory("spark-code-recommendation")
        
        # Prepare tool call parameters
        tool_params = {
            "platform_type": platform_type,
            "platform_params": platform_params
        }
        
        print(f"Calling spark_code_recommendation tool with parameters: {tool_params}")
        
        # Use the real MCP client to call the tool
        with MCPClient(mcp_client_factory) as client:
            response = client.call_tool_sync(
                str(uuid.uuid4()), 
                "spark_code_recommendation", 
                tool_params, 
                timedelta(seconds=180)
            )
            if response["status"] != "success":
                raise Exception("Code recommendation tool call failed! " + str(response))
            
            result = json.loads(response["content"][0]["text"])
            print(f"Code recommendation completed successfully.")
            print(f"DEBUG: Code recommendation result: {json.dumps(result, indent=2)}")
            return result
        
    except Exception as e:
        print(f"Failed to get code recommendations: {str(e)}")
        # Return empty result so the main analysis can still proceed
        return {}


def analyze_spark_workload(platform_type: str, platform_params: dict) -> dict:
    """
    Analyze Spark workload using MCP client.
    
    Args:
        platform_type: Platform type (EMR_EC2, EMR_SERVERLESS, or GLUE)
        platform_params: Platform-specific parameters
        
    Returns:
        Analysis result dictionary
    """
    try:
        # Initialize MCP client provider
        client_provider = MCPClientProvider(
            region=os.environ.get('AWS_REGION')
        )
        
        # Get MCP client
        mcp_client_factory = client_provider.get_client_factory("spark-troubleshooting")
        
        # Prepare tool call parameters
        tool_params = {
            "platform_type": platform_type,
            "platform_params": platform_params
        }
        
        print(f"Calling analyze_spark_workload tool with parameters: {tool_params}")
        
        # Use the real MCP client to call the tool
        with MCPClient(mcp_client_factory) as client:
            response = client.call_tool_sync(
                str(uuid.uuid4()), 
                "analyze_spark_workload", 
                tool_params, 
                timedelta(seconds=180)
            )
            if response["status"] != "success":
                raise Exception("Tool Call failed! " + str(response))
            
            result = json.loads(response["content"][0]["text"])
            print(f"MCP analysis completed successfully. Analysis ID: {result.get('analysis_id', 'N/A')}")
            return result
        
    except Exception as e:
        raise Exception(f"Failed to analyze Spark workload: {str(e)}")


def format_sns_message(platform_type: str, resource_id: str, analysis_result: dict, code_recommendation_result: Optional[dict] = None) -> str:
    """
    Format the analysis result into a readable SNS message.
    
    Args:
        platform_type: Platform type (EMR_EC2, EMR_SERVERLESS, or GLUE)
        resource_id: Resource identifier (e.g., cluster_id/step_id)
        analysis_result: MCP analysis result
        code_recommendation_result: Optional code recommendation result
        
    Returns:
        Formatted message string
    """
    try:
        # Extract key information
        analysis_id = analysis_result.get('analysis_id', 'N/A')
        analysis_status = analysis_result.get('analysis_status', 'N/A')
        analysis_type = analysis_result.get('analysis_type', 'N/A')
        message = analysis_result.get('message', 'N/A')
        
        tool_response = analysis_result.get('tool_response', {})
        analysis_category = tool_response.get('analysis_category', 'N/A')
        root_cause = tool_response.get('root_cause', 'N/A')
        recommendation = tool_response.get('recommendation', 'N/A')
        
        # Build the formatted message
        formatted_message = f"""
🔍 Spark Workload Analysis Complete

📊 Analysis Summary:
• Platform: {platform_type}
• Resource ID: {resource_id}
• Analysis ID: {analysis_id}
• Status: {analysis_status}
• Type: {analysis_type}
• Category: {analysis_category}

📝 Message: {message}

🔍 Root Cause Analysis:
{format_markdown_for_text(root_cause)}

💡 Recommendations:
{format_markdown_for_text(recommendation)}"""
        
        # Add code recommendations if available
        if code_recommendation_result and code_recommendation_result:
            code_recommendations_text = format_code_recommendations(code_recommendation_result)
            if code_recommendations_text:
                formatted_message += f"""

🛠️ Code Recommendations:
{code_recommendations_text}"""
        
        formatted_message += """

---
This analysis was automatically generated by the Spark Troubleshooting Lambda function."""
        
        formatted_message = formatted_message.strip()
        
        return formatted_message
        
    except Exception as e:
        print(f"Error formatting SNS message: {e}")
        # Fallback to JSON format
        return f"Spark Analysis Complete\n\nPlatform: {platform_type}\nResource: {resource_id}\n\nRaw Result:\n{json.dumps(analysis_result, indent=2)}"


def format_error_message(resource_id: str, error: str) -> str:
    """
    Format an error message for SNS.
    
    Args:
        resource_id: Resource identifier
        error: Error message
        
    Returns:
        Formatted error message
    """
    return f"""
❌ Spark Analysis Failed

📊 Request Details:
• Resource ID: {resource_id}

🚨 Error: {error}

---
Please check the Lambda logs for more details.
    """.strip()


def format_code_recommendations(code_recommendation_result: dict) -> str:
    """
    Format code recommendation result for SNS message.
    
    Args:
        code_recommendation_result: Result from spark_code_recommendation tool
        
    Returns:
        Formatted code recommendations text
    """
    try:
        if not code_recommendation_result:
            return ""
        
        # Extract the code_diff which contains the recommendations and optimizations
        code_diff = code_recommendation_result.get('code_diff', '')
        
        if code_diff and code_diff != 'N/A':
            # Format the code diff for better readability in SNS
            formatted_text = format_markdown_for_text(code_diff)
            return formatted_text
        else:
            return ""
        
    except Exception as e:
        print(f"Error formatting code recommendations: {e}")
        # Fallback to JSON format
        return f"Raw Code Recommendations:\n{json.dumps(code_recommendation_result, indent=2)}"


def format_markdown_for_text(text: str) -> str:
    """
    Convert basic markdown formatting to plain text for SNS.
    
    Args:
        text: Text with markdown formatting
        
    Returns:
        Plain text version
    """
    if not text or text == 'N/A':
        return 'N/A'
    
    # Convert markdown to plain text
    formatted = text.replace('#### ', '• ').replace('**', '').replace('* ', '  - ')
    
    # Clean up extra whitespace
    lines = [line.strip() for line in formatted.split('\n') if line.strip()]
    return '\n'.join(lines)


def send_to_sns(topic_arn: str, message: str, platform_type: str, resource_id: str):
    """
    Send message to SNS topic.
    
    Args:
        topic_arn: SNS topic ARN
        message: Message to send
        platform_type: Platform type (for subject)
        resource_id: Resource identifier (for subject)
    """
    try:
        sns_client = boto3.client('sns')
        
        # Construct subject with max 100 characters limit
        subject = f"Spark Analysis [{platform_type}]: {resource_id}"
        if len(subject) > 100:
            # Truncate resource_id to fit within 100 chars
            max_resource_id_len = 100 - len(f"Spark Analysis [{platform_type}]: ") - 3  # 3 for "..."
            subject = f"Spark Analysis [{platform_type}]: {resource_id[:max_resource_id_len]}..."
        
        response = sns_client.publish(
            TopicArn=topic_arn,
            Message=message,
            Subject=subject
        )
        
        print(f"SNS message sent successfully. MessageId: {response['MessageId']}")
        
    except Exception as e:
        print(f"Failed to send SNS message: {e}")
        raise


class MCPClientProvider:
    """
    MCP Client Provider for Lambda environment.
    Simplified version without AWS profile support (uses IAM roles instead).
    """
    
    def __init__(self, region: str = "us-east-2"):
        """
        Initialize MCP client provider.
        
        Args:
            stage: Deployment stage (e.g., 'beta', 'prod')
            region: AWS region
            profile_name: Not used in Lambda (uses IAM roles)
        """
        self.region = region

    def get_client_factory(self, server_name):
        """
        Get or create an MCP client.
        
        Returns:
            MCP client instance
        """
        try:
            # Construct credentials from environment variables
            credentials = Credentials(
                access_key=os.environ.get('AWS_ACCESS_KEY_ID'),
                secret_key=os.environ.get('AWS_SECRET_ACCESS_KEY'),
                token=os.environ.get('AWS_SESSION_TOKEN')
            )
            
            mcp_client_factory = lambda: aws_iam_streamablehttp_client(
                endpoint=f"https://sagemaker-unified-studio-mcp.{self.region}.api.aws/{server_name}/mcp",
                aws_region=self.region,
                aws_service="sagemaker-unified-studio-mcp",
                credentials=credentials
            )
            # Return actual MCP client using the Python executable and proxy path
            return mcp_client_factory
            
        except ImportError as e:
            raise ImportError(f"Could not import MCP dependencies: {e}. Please ensure the required packages are included in the Lambda deployment package.")

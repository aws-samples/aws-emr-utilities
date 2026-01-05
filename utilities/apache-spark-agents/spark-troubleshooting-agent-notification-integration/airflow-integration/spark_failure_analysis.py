"""
Spark Failure Analysis - AI-powered failure analysis for Spark jobs.

Usage:
    from dags.spark_failure_analysis import spark_failure_callback

    # With AI agent (default)
    EmrServerlessStartJobOperator(
        task_id="my_spark_job",
        application_id="...",
        on_failure_callback=spark_failure_callback(),
    )

    # Without AI agent (when lacking Bedrock permissions)
    GlueJobOperator(
        task_id="my_glue_job",
        job_name="...",
        on_failure_callback=spark_failure_callback(agent=False),
    )

When a Spark operator fails, the callback triggers a separate analysis DAG that:
- Runs AI-powered failure analysis using job details from the failed operator
- Sends results to Slack

When agent=False, the analysis calls MCP tools directly without using the AI agent.

Requirements:
    - apache-airflow-providers-amazon
    - apache-airflow-providers-slack
    - strands-agents (only when agent=True)
    - mcp-proxy-for-aws
    - AWS connection configured in Airflow (default: "aws_default")
    - Slack webhook connection configured in Airflow (default: "slack_default")
"""

import json
import logging
import os
import time
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Literal, Optional

import boto3
from airflow import DAG
from airflow.providers.amazon.aws.hooks.base_aws import AwsBaseHook
from airflow.providers.slack.notifications.slack_webhook import SlackWebhookNotifier
from airflow.sdk import task
from airflow.sdk.execution_time.comms import TriggerDagRun
from airflow.sdk.execution_time.task_runner import SUPERVISOR_COMMS
from botocore.config import Config
from mcp_proxy_for_aws.client import aws_iam_streamablehttp_client
from pydantic import BaseModel
from strands import Agent
from strands.hooks import (
    AfterToolCallEvent,
    BeforeToolCallEvent,
    HookProvider,
    HookRegistry,
)
from strands.models import BedrockModel
from strands.tools.mcp import MCPClient

logger = logging.getLogger(__name__)

# Configurable via environment variables
BEDROCK_MODEL_ID = os.environ.get(
    "BEDROCK_MODEL_ID", "us.anthropic.claude-3-7-sonnet-20250219-v1:0"
)
MCP_ENDPOINT_TEMPLATE = os.environ.get(
    "MCP_ENDPOINT_TEMPLATE",
    "https://sagemaker-unified-studio-mcp.{region}.api.aws/{server_name}/mcp",
)

PlatformType = Literal["emr_serverless", "glue", "emr_ec2"]

SPARK_OPERATORS: dict[str, PlatformType] = {
    "EmrServerlessStartJobOperator": "emr_serverless",
    "GlueJobOperator": "glue",
    "EmrAddStepsOperator": "emr_ec2",
}


class SparkAnalysisOutput(BaseModel):
    issue_summary: str | None = None
    root_cause: str
    recommendation: str
    code_diff: str | None = None


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


class MCPSerializationFixHook(HookProvider):
    """
    Hook to fix MCP serialization issues for specific tools and parameters.

    This hook intercepts tool calls before they are sent to the MCP server
    and fixes known serialization issues where arrays are being JSON-stringified.
    """

    # Tools and their parameters that need dictionary deserialization
    TOOLS_WITH_DICT_PARAMS = {
        "spark_code_recommendation": ["source_code", "context", "platform_params"],
        "analyze_spark_workload": ["platform_params"],
    }

    def __init__(self):
        super().__init__()
        logger.info("MCPSerializationFixHook initialized")

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        """Register the hook callbacks with the registry."""
        registry.add_callback(BeforeToolCallEvent, self._fix_tool_arguments)

    def _fix_tool_arguments(self, event: BeforeToolCallEvent) -> None:
        """
        Fix serialization issues before tool invocation.

        Args:
            event: The before tool invocation event
        """
        tool_name = event.tool_use.get("name")

        if tool_name not in self.TOOLS_WITH_DICT_PARAMS:
            return

        logger.info(f"Applying serialization fix for tool: {tool_name}")

        # Fix the tool_use input parameters
        tool_input = event.tool_use.get("input", {})
        original_input = tool_input.copy()

        dict_params = self.TOOLS_WITH_DICT_PARAMS[tool_name]
        self._fix_dict_parameters(tool_input, dict_params, tool_name)

        if tool_input != original_input:
            logger.info(
                f"Fixed parameters for {tool_name}: {original_input} -> {tool_input}"
            )

    def _fix_dict_parameters(
        self, params: Dict[str, Any], dict_param_names: List[str], tool_name: str
    ) -> None:
        """
        Fix dictionary parameters that have been JSON-stringified in place.

        Args:
            params: The tool parameters (modified in place)
            dict_param_names: List of parameter names that should be dictionaries
            tool_name: Name of the tool (for logging)
        """
        for param_name in dict_param_names:
            if param_name in params:
                param_value = params[param_name]
                # Check if the parameter is a JSON string that should be a dictionary
                if isinstance(param_value, str):
                    try:
                        # Try to parse as JSON
                        parsed_value = json.loads(param_value)
                        # If it's a dict, use the parsed value
                        if isinstance(parsed_value, dict):
                            logger.info(
                                f"Fixed {tool_name}.{param_name}: '{param_value}' -> {parsed_value}"
                            )
                            params[param_name] = parsed_value
                        else:
                            logger.warning(
                                f"Parameter {tool_name}.{param_name} is JSON but not a dictionary: {parsed_value}"
                            )
                    except json.JSONDecodeError:
                        # If it's not valid JSON, leave it as is
                        logger.debug(
                            f"Parameter {tool_name}.{param_name} is not JSON, leaving as string: {param_value}"
                        )


SYSTEM_PROMPT = """You are a Spark troubleshooting agent. Your purpose is to help debug and diagnose issues in Apache Spark workloads.

If analyze_spark_workload recommends calling spark_code_recommendation, you MUST call it.

Your response will be sent as a Slack message. Populate the structured output fields:
- issue_summary: Brief description of what went wrong
- root_cause: The underlying cause of the failure
- recommendation: Steps to fix the issue
- code_diff: If spark_code_recommendation was called, include the diff verbatim. Otherwise leave empty.

Do not use markdown formatting (no #, ##, **, etc.). Use plain text only. Do not ask follow-up questions."""


class SparkTroubleshootingAgent:
    def __init__(
        self,
        session: boto3.Session,
        mcp_clients: List[Any] = [],
        logger: logging.Logger = logger,
        use_agent: bool = True,
    ):
        """
        Initialize Spark Troubleshooting Agent.

        Args:
            session: AWS Boto3 Session. Must have InvokeModel permission on Claude Sonnet 3.7.
            mcp_clients: List of MCP clients to use with the agent
            logger: Logger instance for logging
            use_agent: If True, use AI agent. If False, call MCP tools directly.
        """
        self.mcp_clients = mcp_clients
        self.logger = logger
        self.use_agent = use_agent

        # Initialize tracking dictionaries for tool calls and responses
        self._tool_calls: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        self._tool_responses: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

        # Create the agent only if use_agent is True
        self.agent = None
        if self.use_agent:
            self._initialize_agent(session)

    def _initialize_agent(self, session: boto3.Session):
        """Initialize the Strands agent with MCP tools and callbacks."""
        # Setup bedrock model
        bedrock_cfg = Config(
            connect_timeout=10,
            read_timeout=180,
            tcp_keepalive=True,
            max_pool_connections=64,
            retries={"max_attempts": 10, "mode": "adaptive"},
        )

        # Initialize Bedrock model
        bedrock_model = BedrockModel(
            model_id=BEDROCK_MODEL_ID,
            boto_client_config=bedrock_cfg,
            boto_session=session,
            temperature=0,
        )

        # Define callbacks for tool invocation tracking
        def before_tool_invocation_callback(event: BeforeToolCallEvent) -> None:
            if event.tool_use:
                tool_name = event.tool_use.get("name", "unknown")
                tool_params = event.tool_use.get("input", {})

                # Record the tool call
                self._tool_calls[tool_name].append(
                    {"parameters": tool_params, "timestamp": time.time()}
                )

                self.logger.info(f"Tool invoked: {tool_name}")
                self.logger.info(f"Tool parameters: {tool_params}")

        def after_tool_invocation_callback(event: AfterToolCallEvent) -> None:
            if event.selected_tool:
                tool_name = event.selected_tool.tool_name

                # Record the tool response
                response_data = {
                    "result": event.result,
                    "timestamp": time.time(),
                }
                self._tool_responses[tool_name].append(response_data)

        # Create the agent
        self.agent = Agent(
            name="sagemaker_unified_studio_spark_troubleshooting_agent",
            model=bedrock_model,
            system_prompt=SYSTEM_PROMPT,
            tools=self.mcp_clients,
            hooks=[MCPSerializationFixHook()],
        )

        # Add callbacks
        self.agent.hooks.add_callback(
            BeforeToolCallEvent, before_tool_invocation_callback
        )
        self.agent.hooks.add_callback(
            AfterToolCallEvent, after_tool_invocation_callback
        )

        self.logger.info("Agent initialized with callbacks")

    def _run_direct_tool_calls(
        self, platform_type: PlatformType, platform_params: dict
    ) -> "SparkAnalysisOutput":
        """
        Run MCP tools directly without using the agent.

        Args:
            platform_type: The platform type (emr_serverless, glue, emr_ec2)
            platform_params: Platform-specific parameters

        Returns:
            SparkAnalysisOutput with analysis results
        """
        mcp_platform_type = platform_type.upper()  # emr_serverless -> EMR_SERVERLESS

        # Call analyze_spark_workload
        tool_params = {
            "platform_type": mcp_platform_type,
            "platform_params": platform_params,
        }

        self.logger.info(f"Calling analyze_spark_workload with params: {tool_params}")

        with self.mcp_clients[0] as client:
            response = client.call_tool_sync(
                str(uuid.uuid4()),
                "analyze_spark_workload",
                tool_params,
                timedelta(seconds=180),
            )
            if response["status"] != "success":
                raise Exception("analyze_spark_workload failed: " + str(response))
            analysis_result = json.loads(response["content"][0]["text"])

        self.logger.info(f"analyze_spark_workload result: {analysis_result}")

        tool_response = analysis_result.get("tool_response", {})
        root_cause = tool_response.get("root_cause", "Unable to determine root cause")
        recommendation = tool_response.get(
            "recommendation", "No recommendation available"
        )

        # Check if we need code recommendations
        code_diff = None
        if _should_get_code_recommendations(analysis_result):
            self.logger.info("Getting code recommendations...")
            with self.mcp_clients[1] as client:
                code_params = {
                    "platform_type": mcp_platform_type,
                    "platform_params": platform_params,
                }
                response = client.call_tool_sync(
                    str(uuid.uuid4()),
                    "spark_code_recommendation",
                    code_params,
                    timedelta(seconds=180),
                )
                if response["status"] != "success":
                    self.logger.error(f"spark_code_recommendation failed: {response}")
                else:
                    code_result = json.loads(response["content"][0]["text"])
                    code_diff = code_result.get("code_diff")

        # Build generic issue summary
        issue_summary = (
            f"Spark job failure on {platform_type.replace('_', ' ').upper()}"
        )
        if "application_id" in platform_params:
            issue_summary += f" (Application: {platform_params['application_id']})"
        elif "job_name" in platform_params:
            issue_summary += f" (Job: {platform_params['job_name']})"
        elif "cluster_id" in platform_params:
            issue_summary += f" (Cluster: {platform_params['cluster_id']})"

        return SparkAnalysisOutput(
            issue_summary=issue_summary,
            root_cause=root_cause,
            recommendation=recommendation,
            code_diff=code_diff,
        )

    def __call__(
        self,
        prompt: str | None = None,
        platform_type: str | None = None,
        platform_params: dict | None = None,
        **kwargs,
    ):
        """
        Run analysis.

        When use_agent=True: Uses the AI agent with the given prompt
        When use_agent=False: Calls MCP tools directly with platform_type and platform_params

        Args:
            prompt: The query/prompt to send to the agent (required when use_agent=True)
            platform_type: Platform type for direct calls (required when use_agent=False)
            platform_params: Platform parameters for direct calls (required when use_agent=False)

        Returns:
            Agent response or SparkAnalysisOutput depending on mode
        """
        # Clear previous tool call and response tracking
        self._tool_calls.clear()
        self._tool_responses.clear()

        if not self.use_agent:
            if not platform_type or not platform_params:
                raise ValueError(
                    "platform_type and platform_params required when agent is disabled"
                )
            return self._run_direct_tool_calls(platform_type, platform_params)

        if not self.agent:
            raise RuntimeError("Agent is not initialized")

        self.logger.info(f"Running query: {prompt[:100]}...")
        try:
            response = self.agent(prompt, **kwargs)
            return response
        except Exception as e:
            self.logger.error(f"Error running agent: {str(e)}", exc_info=True)
            raise

    def get_tool_calls(
        self, tool_name: Optional[str] = None
    ) -> Dict[str, List[Dict[str, Any]]]:
        """
        Get recorded tool calls.

        Args:
            tool_name: Optional tool name to filter by

        Returns:
            dict: Dictionary of tool calls
        """
        if tool_name:
            return {tool_name: self._tool_calls.get(tool_name, [])}
        return dict(self._tool_calls)

    def get_tool_responses(
        self, tool_name: Optional[str] = None
    ) -> Dict[str, List[Dict[str, Any]]]:
        """
        Get recorded tool responses.

        Args:
            tool_name: Optional tool name to filter by

        Returns:
            dict: Dictionary of tool responses
        """
        if tool_name:
            return {tool_name: self._tool_responses.get(tool_name, [])}
        return dict(self._tool_responses)


def _get_mcp_client_factory(server_name: str, credentials, region: str):
    """Create an MCP client factory for the given server."""
    endpoint = MCP_ENDPOINT_TEMPLATE.format(region=region, server_name=server_name)
    return lambda: aws_iam_streamablehttp_client(
        endpoint=endpoint,
        aws_region=region,
        aws_service="sagemaker-unified-studio-mcp",
        credentials=credentials,
    )


def _get_latest_failed_emr_step(cluster_id: str, session: boto3.Session) -> str | None:
    """Query EMR for the most recent failed step on a cluster."""
    client = session.client("emr")

    response = client.list_steps(ClusterId=cluster_id, StepStates=["FAILED"])
    steps = response.get("Steps", [])
    if steps:
        return steps[0].get("Id")

    response = client.list_steps(ClusterId=cluster_id)
    steps = response.get("Steps", [])
    return steps[0].get("Id") if steps else None


def _format_slack_message(platform_type: str, analysis: SparkAnalysisOutput) -> str:
    """Format the Slack message using emojis as headers."""
    header = f"⚠️ *{platform_type.replace('_', ' ').upper()} Spark Job Failed*"

    sections = [
        header,
        "",
        f"🔍 Issue Summary\n{analysis.issue_summary}",
        "",
        f"🎯 Root Cause\n{analysis.root_cause}",
        "",
        f"💡 Recommendation\n{analysis.recommendation}",
    ]

    if analysis.code_diff:
        sections.extend(
            [
                "",
                f"📝 Code Diff\n```\n{analysis.code_diff}\n```",
            ]
        )

    return "\n".join(sections)


def spark_failure_callback(agent: bool = True):
    """
    Create a callback to trigger Spark failure analysis DAG.

    Args:
        agent: If True (default), use AI agent. If False, call MCP tools directly.

    Usage:
        EmrServerlessStartJobOperator(
            task_id="my_spark_job",
            application_id="...",
            on_failure_callback=spark_failure_callback(),  # agent enabled (default)
        )

        GlueJobOperator(
            task_id="my_glue_job",
            on_failure_callback=spark_failure_callback(agent=False),  # agent disabled
        )
    """

    def _callback(context: dict) -> None:
        task_instance = context["task"]
        operator_name = type(task_instance).__name__

        if operator_name not in SPARK_OPERATORS:
            logger.warning(
                f"spark_failure_callback called on non-Spark operator: {operator_name}"
            )
            return

        platform_type = SPARK_OPERATORS[operator_name]
        logger.info(
            f"Triggering analysis for failed {platform_type} task: {task_instance.task_id}"
        )

        conf: dict = {"platform_type": platform_type, "use_agent": agent}

        if platform_type == "emr_serverless":
            conf["application_id"] = task_instance.application_id
            conf["job_id"] = getattr(task_instance, "job_id", None)
        elif platform_type == "glue":
            conf["job_name"] = task_instance.job_name
            conf["job_id"] = getattr(task_instance, "_job_run_id", None)
        elif platform_type == "emr_ec2":
            conf["cluster_id"] = task_instance.job_flow_id

            aws_hook = AwsBaseHook(aws_conn_id="aws_default")
            credentials = aws_hook.get_credentials()
            region = aws_hook.region_name or "us-east-1"

            # Create a boto3 session
            session = boto3.Session(
                aws_access_key_id=credentials.access_key,
                aws_secret_access_key=credentials.secret_key,
                aws_session_token=credentials.token,
                region_name=region,
            )

            conf["step_id"] = _get_latest_failed_emr_step(
                task_instance.job_flow_id, session
            )

        dag_id = context["dag"].dag_id
        task_id = task_instance.task_id
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        trigger = TriggerDagRun(
            dag_id="spark_failure_analysis",
            run_id=f"analysis_{dag_id}_{task_id}_{timestamp}",
            logical_date=datetime.now(timezone.utc),
            conf=conf,
        )

        response = SUPERVISOR_COMMS.send(msg=trigger)
        logger.info(f"Triggered spark_failure_analysis DAG: {response}")

    return _callback


# =============================================================================
# Analysis DAG - Triggered by spark_failure_callback
# =============================================================================

with DAG(
    dag_id="spark_failure_analysis",
    start_date=datetime(2024, 1, 1),
    schedule=None,
    catchup=False,
    max_active_runs=1,
    tags=["spark", "analysis"],
) as dag:

    @task
    def run_analysis(**context):
        conf = context["dag_run"].conf
        platform_type = conf["platform_type"]
        application_id = conf.get("application_id")
        job_id = conf.get("job_id")
        job_name = conf.get("job_name")
        cluster_id = conf.get("cluster_id")
        step_id = conf.get("step_id")
        airflow_context = context
        use_agent = conf.get("use_agent", True)
        aws_conn_id = "aws_default"
        slack_conn_id = "slack_default"

        aws_hook = AwsBaseHook(aws_conn_id=aws_conn_id)
        credentials = aws_hook.get_credentials()
        region = aws_hook.region_name or "us-east-1"

        # Create a boto3 session
        session = boto3.Session(
            aws_access_key_id=credentials.access_key,
            aws_secret_access_key=credentials.secret_key,
            aws_session_token=credentials.token,
            region_name=region,
        )

        if platform_type == "emr_serverless":
            if not application_id or not job_id:
                raise ValueError(
                    "application_id and job_id are required for emr_serverless platform"
                )
            query = f"Help me debug my EMR serverless job from application {application_id} with job id {job_id}"
            platform_params = {"application_id": application_id, "job_run_id": job_id}

        elif platform_type == "glue":
            if not job_name or not job_id:
                raise ValueError("job_name and job_id are required for glue platform")
            query = f"Help me debug my AWS Glue job named {job_name} with job run id {job_id}"
            platform_params = {"job_name": job_name, "job_run_id": job_id}

        elif platform_type == "emr_ec2":
            if not cluster_id:
                raise ValueError("cluster_id is required for emr_ec2 platform")
            if not step_id:
                raise ValueError(f"No failed step found for EMR cluster {cluster_id}")
            query = f"Help me debug my EMR step with cluster id {cluster_id} and step id {step_id}"
            platform_params = {"cluster_id": cluster_id, "step_id": step_id}

        else:
            raise ValueError(f"Unsupported platform_type: {platform_type}")

        logger.info(f"Running analysis (use_agent={use_agent})")

        spark_troubleshooting = MCPClient(
            _get_mcp_client_factory("spark-troubleshooting", credentials, region)
        )
        spark_code_recommendation = MCPClient(
            _get_mcp_client_factory("spark-code-recommendation", credentials, region)
        )

        troubleshooter = SparkTroubleshootingAgent(
            session=session,
            mcp_clients=[spark_troubleshooting, spark_code_recommendation],
            use_agent=use_agent,
        )

        if use_agent:
            logger.info(f"Running agent with query: {query}")
            response = troubleshooter(
                query, structured_output_model=SparkAnalysisOutput
            )
            tool_responses = troubleshooter.get_tool_responses()
            logger.info(tool_responses)
            if not tool_responses:
                logger.error("No tools were called during analysis")
                return {
                    "platform_type": platform_type,
                    "application_id": application_id,
                    "job_name": job_name,
                    "cluster_id": cluster_id,
                    "job_id": job_id,
                    "step_id": step_id,
                    "status": "no_tools_called",
                }
            analysis: SparkAnalysisOutput = response.structured_output
        else:
            logger.info(
                f"Running direct tool calls with platform_params: {platform_params}"
            )
            analysis = troubleshooter(
                platform_type=platform_type, platform_params=platform_params
            )

        slack_message = _format_slack_message(platform_type, analysis)

        notifier = SlackWebhookNotifier(
            slack_webhook_conn_id=slack_conn_id, text=slack_message
        )
        notifier.notify(airflow_context or {})

        logger.info("Analysis complete, Slack notification sent")

        return {
            "platform_type": platform_type,
            "application_id": application_id,
            "job_name": job_name,
            "cluster_id": cluster_id,
            "job_id": job_id,
            "step_id": step_id,
            "status": "analyzed",
        }

    run_analysis()

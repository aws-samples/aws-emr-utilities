"""Lambda handler — wraps the MCP ASGI app with Mangum."""
import os
os.environ.setdefault("MCP_TRANSPORT", "streamable-http")

from mangum import Mangum
from spark_advisor_mcp import mcp


def handler(event, context):
    # Reset cached session manager so streamable_http_app() creates a fresh
    # one. MCP's StreamableHTTPSessionManager refuses to restart its lifespan
    # after Mangum shuts it down, crashing on the 2nd invocation in the same
    # Lambda container.
    mcp._session_manager = None
    app = mcp.streamable_http_app()
    return Mangum(app, lifespan="auto")(event, context)

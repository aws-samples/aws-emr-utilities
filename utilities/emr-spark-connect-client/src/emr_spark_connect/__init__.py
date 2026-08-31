# // Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# // SPDX-License-Identifier: MIT-0
"""EMR Spark Connect — unified client for EMR Serverless, EMR on EKS, and EMR on EC2."""

from .backends import EndpointExpiredError
from .session import EMRSparkSession

__all__ = ["EMRSparkSession", "EndpointExpiredError"]
__version__ = "0.1.0"

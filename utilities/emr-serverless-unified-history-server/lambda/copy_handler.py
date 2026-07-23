"""
S3 Event-Driven Spark Event Log Copy Lambda

Triggered by S3 Event Notifications (s3:ObjectCreated:*) on the source bucket where
EMR Serverless writes job logs. Copies ONLY Spark event logs (objects under a
`sparklogs/` path segment) to the destination bucket, flattening them into a single
Spark History Server log directory:

    source:      logs/applications/<app-id>/jobs/<job-run-id>/sparklogs/eventlog_v2_<job-run-id>/events_0_<job-run-id>
    destination: logs/eventlog_v2_<job-run-id>/events_0_<job-run-id>

This lets one Spark History Server (spark.history.fs.logDirectory=s3://<dest>/logs/)
load applications from many EMR Serverless applications and job runs. All other log
objects (driver/executor stderr/stdout, archived, job-metadata) are skipped.

Environment variables:
    DESTINATION_BUCKET  (required) - target bucket name
    DESTINATION_PREFIX  (optional) - SHS log directory prefix (default: "logs/")

Memory: 128 MB | Timeout: 60s
"""

import json
import logging
import os
import urllib.parse

import boto3
from botocore.config import Config

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3 = boto3.client("s3", config=Config(retries={"max_attempts": 3, "mode": "adaptive"}))

DESTINATION_BUCKET = os.environ["DESTINATION_BUCKET"]
DESTINATION_PREFIX = os.environ.get("DESTINATION_PREFIX", "logs/")

SPARKLOGS_MARKER = "/sparklogs/"

# copy_object supports up to 5 GiB. Larger objects need multipart UploadPartCopy.
COPY_OBJECT_MAX_BYTES = 5 * 1024 * 1024 * 1024


def destination_key_for(source_key):
    """Map a source key to its flattened SHS destination key, or None to skip."""
    idx = source_key.find(SPARKLOGS_MARKER)
    if idx == -1:
        return None
    return DESTINATION_PREFIX + source_key[idx + len(SPARKLOGS_MARKER):]


def lambda_handler(event, context):
    """Process S3 event notification records. Logs and continues on per-object errors."""
    records = event.get("Records", [])
    copied, skipped, failed = 0, 0, 0

    for record in records:
        try:
            source_bucket = record["s3"]["bucket"]["name"]
            source_key = urllib.parse.unquote_plus(record["s3"]["object"]["key"])
            size = record["s3"]["object"].get("size", 0)
        except KeyError:
            logger.error("Malformed record, skipping: %s", json.dumps(record))
            failed += 1
            continue

        destination_key = destination_key_for(source_key)
        if destination_key is None:
            skipped += 1
            logger.info("Skipped (not a spark event log): s3://%s/%s", source_bucket, source_key)
            continue

        try:
            if size > COPY_OBJECT_MAX_BYTES:
                _multipart_copy(source_bucket, source_key, destination_key, size)
            else:
                s3.copy_object(
                    CopySource={"Bucket": source_bucket, "Key": source_key},
                    Bucket=DESTINATION_BUCKET,
                    Key=destination_key,
                )
            copied += 1
            logger.info(
                "Copied s3://%s/%s -> s3://%s/%s (%d bytes)",
                source_bucket, source_key, DESTINATION_BUCKET, destination_key, size,
            )
        except Exception:
            failed += 1
            logger.exception(
                "Failed to copy s3://%s/%s -> s3://%s/%s",
                source_bucket, source_key, DESTINATION_BUCKET, destination_key,
            )

    result = {"copied": copied, "skipped": skipped, "failed": failed, "total": len(records)}
    logger.info("Batch complete: %s", json.dumps(result))
    return result


def _multipart_copy(source_bucket, source_key, destination_key, size):
    """Multipart server-side copy for objects > 5 GiB."""
    part_size = 1 * 1024 * 1024 * 1024  # 1 GiB parts
    mpu = s3.create_multipart_upload(Bucket=DESTINATION_BUCKET, Key=destination_key)
    upload_id = mpu["UploadId"]
    try:
        parts = []
        part_number = 1
        offset = 0
        while offset < size:
            last_byte = min(offset + part_size, size) - 1
            resp = s3.upload_part_copy(
                Bucket=DESTINATION_BUCKET,
                Key=destination_key,
                UploadId=upload_id,
                PartNumber=part_number,
                CopySource={"Bucket": source_bucket, "Key": source_key},
                CopySourceRange=f"bytes={offset}-{last_byte}",
            )
            parts.append({"ETag": resp["CopyPartResult"]["ETag"], "PartNumber": part_number})
            offset = last_byte + 1
            part_number += 1
        s3.complete_multipart_upload(
            Bucket=DESTINATION_BUCKET,
            Key=destination_key,
            UploadId=upload_id,
            MultipartUpload={"Parts": parts},
        )
    except Exception:
        s3.abort_multipart_upload(
            Bucket=DESTINATION_BUCKET, Key=destination_key, UploadId=upload_id
        )
        raise

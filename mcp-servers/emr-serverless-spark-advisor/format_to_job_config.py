#!/usr/bin/env python3
"""
Format EMR Serverless recommendations to job configuration format.
"""

import json
import sys
from pathlib import Path
from datetime import datetime


def format_to_job_config(recommendation: dict, job_name: str = None) -> dict:
    """Convert recommendation to job configuration format."""
    
    app_name = recommendation.get('application_name', 'unknown-app')
    if job_name is None:
        job_name = f"{app_name}-job"
    
    spark_configs = recommendation.get('spark_configs', {})
    
    # Build job configuration
    job_config = {
        "job_name": job_name,
        "job_id": None,
        "ams_app_name": app_name,
        "created_by": None,
        "configuration": {
            "config_name": f"{app_name}-config",
            "ams_app_name": app_name,
            "version": None,
            "created_by": None,
            "updated_by": None,
            "stop_job_after_minutes": None,
            "type": "spark",
            "created_timestamp": datetime.utcnow().isoformat() + "Z",
            "updated_timestamp": datetime.utcnow().isoformat() + "Z",
            "schedule": {
                "enabled": False
            },
            "vault_enabled": False,
            "compute_platform": "EMR_SERVERLESS",
            "compute_platform_properties": {
                "gpu_enabled": False,
                "spot_toleration": None,
                "graviton_enabled": None,
                "emr_serverless_application_label": None
            },
            "spark_conf": spark_configs,
            "hadoop_conf": {},
            "main_application_file": None,
            "gpu_enabled": False,
            "spot_toleration": None,
            "main_class": None,
            "language": None
        },
        "config_name": f"{app_name}-config",
        "job_status": {
            "status_descriptor": None,
            "details": None,
            "links": []
        },
        "job_created_at": None,
        "job_deployed_at": None,
        "job_ended_at": None,
        "properties": {},
        "create_namespace": False,
        "job_priority": None
    }
    
    return job_config


def format_recommendations_file(input_file: Path, output_file: Path):
    """Format all recommendations in a file to job config format."""
    
    with open(input_file) as f:
        recommendations = json.load(f)
    
    formatted_jobs = []
    for rec in recommendations:
        app_name = rec.get('application_name', 'unknown-app')
        job_name = f"{app_name}-recommended-job"
        formatted = format_to_job_config(rec, job_name)
        formatted_jobs.append(formatted)
    
    with open(output_file, 'w') as f:
        json.dump(formatted_jobs, f, indent=2)
    
    print(f"✓ Formatted {len(formatted_jobs)} recommendations to {output_file}")


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Format recommendations to job config format")
    parser.add_argument("--input", required=True, help="Input recommendations JSON file")
    parser.add_argument("--output", required=True, help="Output job config JSON file")
    
    args = parser.parse_args()
    
    format_recommendations_file(Path(args.input), Path(args.output))

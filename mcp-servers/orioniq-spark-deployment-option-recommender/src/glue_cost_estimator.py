"""
AWS Glue Cost Estimator

Maps Spark executor configurations to Glue DPU-based pricing.

Worker types (from https://docs.aws.amazon.com/glue/latest/dg/worker-types.html):
  G-series (general purpose):
    G.1X:  1 DPU  (4 vCPU, 16GB,  44GB free disk) — 1 executor/node
    G.2X:  2 DPU  (8 vCPU, 32GB,  78GB free disk) — 1 executor/node
    G.4X:  4 DPU  (16 vCPU, 64GB, 230GB free disk) — 1 executor/node
    G.8X:  8 DPU  (32 vCPU, 128GB, 485GB free disk) — 1 executor/node
    G.12X: 12 DPU (48 vCPU, 192GB, 741GB free disk) — 1 executor/node
    G.16X: 16 DPU (64 vCPU, 256GB, 996GB free disk) — 1 executor/node
  R-series (memory-optimized, uses M-DPUs with 2x memory):
    R.1X:  1 M-DPU (4 vCPU, 32GB,  44GB free disk) — 1 executor/node
    R.2X:  2 M-DPU (8 vCPU, 64GB,  78GB free disk) — 1 executor/node
    R.4X:  4 M-DPU (16 vCPU, 128GB, 230GB free disk) — 1 executor/node
    R.8X:  8 M-DPU (32 vCPU, 256GB, 485GB free disk) — 1 executor/node

Key: Each Glue worker runs exactly 1 Spark executor. So num_workers = num_executors.

Pricing: $0.44/DPU-hour (us-east-1), billed per-second with 1-min minimum.
"""

import logging
import boto3
import json
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)

# Glue worker specs: vcpus, memory_gb, dpus, disk_free_gb, category
# Each worker runs exactly 1 Spark executor
WORKER_TYPES = {
    # G-series: general purpose
    'G.025X': {'vcpus': 2,  'memory_gb': 4,   'dpus': 0.25, 'disk_gb': 34,  'category': 'general'},
    'G.1X':  {'vcpus': 4,  'memory_gb': 16,  'dpus': 1,  'disk_gb': 44,  'category': 'general'},
    'G.2X':  {'vcpus': 8,  'memory_gb': 32,  'dpus': 2,  'disk_gb': 78,  'category': 'general'},
    'G.4X':  {'vcpus': 16, 'memory_gb': 64,  'dpus': 4,  'disk_gb': 230, 'category': 'general'},
    'G.8X':  {'vcpus': 32, 'memory_gb': 128, 'dpus': 8,  'disk_gb': 485, 'category': 'general'},
    'G.12X': {'vcpus': 48, 'memory_gb': 192, 'dpus': 12, 'disk_gb': 741, 'category': 'general'},
    'G.16X': {'vcpus': 64, 'memory_gb': 256, 'dpus': 16, 'disk_gb': 996, 'category': 'general'},
    # R-series: memory-optimized (M-DPUs — 2x memory per DPU)
    'R.1X':  {'vcpus': 4,  'memory_gb': 32,  'dpus': 1,  'disk_gb': 44,  'category': 'memory_optimized'},
    'R.2X':  {'vcpus': 8,  'memory_gb': 64,  'dpus': 2,  'disk_gb': 78,  'category': 'memory_optimized'},
    'R.4X':  {'vcpus': 16, 'memory_gb': 128, 'dpus': 4,  'disk_gb': 230, 'category': 'memory_optimized'},
    'R.8X':  {'vcpus': 32, 'memory_gb': 256, 'dpus': 8,  'disk_gb': 485, 'category': 'memory_optimized'},
}

FALLBACK_DPU_RATE = 0.44        # Standard G-series
FALLBACK_FLEX_DPU_RATE = 0.29   # Flex (G.1X and G.2X only, ~34% discount)
FALLBACK_MDPU_RATE = 0.52       # Memory-optimized R-series (M-DPUs)
FLEX_ELIGIBLE_WORKERS = {'G.1X', 'G.2X'}  # Only these support Flex execution


def pick_worker_type(executor_cores: int, executor_memory_gb: float, workload_type: str = 'balanced') -> str:
    """Pick the smallest Glue worker type that fits the executor config.
    Prefers R-series for memory-intensive workloads, G-series otherwise."""
    # Determine which series to prefer
    prefer_memory = workload_type.lower() in ('memory_intensive', 'memory-intensive', 'memory')
    
    # Try preferred series first, then fallback
    series_order = ['memory_optimized', 'general'] if prefer_memory else ['general', 'memory_optimized']
    
    for category in series_order:
        for wtype, spec in WORKER_TYPES.items():
            if spec['category'] != category:
                continue
            if spec['vcpus'] >= executor_cores and spec['memory_gb'] >= executor_memory_gb:
                return wtype
    
    return 'G.16X'  # largest if nothing fits


def calculate_glue_workers(executor_count: int, executor_cores: int, executor_memory_gb: float, workload_type: str = 'balanced') -> Dict[str, Any]:
    """Calculate Glue workers needed. Each Glue worker = 1 Spark executor."""
    worker_type = pick_worker_type(executor_cores, executor_memory_gb, workload_type)
    spec = WORKER_TYPES[worker_type]

    # 1 executor per worker in Glue, so num_workers = num_executors
    num_workers = max(2, executor_count)  # Glue minimum is 2 workers
    total_dpus = num_workers * spec['dpus']

    return {
        'worker_type': worker_type,
        'num_workers': num_workers,
        'dpus_per_worker': spec['dpus'],
        'total_dpus': total_dpus,
        'worker_specs': f"{spec['vcpus']} vCPU, {spec['memory_gb']}GB RAM, {spec['disk_gb']}GB disk",
    }


def fetch_glue_rates(region: str) -> Dict[str, float]:
    """Fetch Glue DPU-hour rates from AWS Pricing API.
    Returns standard, flex, and memory-optimized (M-DPU) rates."""
    rates = {
        'standard': FALLBACK_DPU_RATE,
        'flex': FALLBACK_FLEX_DPU_RATE,
        'memory_optimized': FALLBACK_MDPU_RATE,
    }
    try:
        from combined_emr_advisor_mcp import load_region_mapping
        region_mapping = load_region_mapping()
        location = region_mapping.get(region, 'US East (N. Virginia)')

        pricing_client = boto3.client('pricing', region_name='us-east-1')
        resp = pricing_client.get_products(
            ServiceCode='AWSGlue', MaxResults=20,
            Filters=[
                {'Type': 'TERM_MATCH', 'Field': 'location', 'Value': location},
            ],
        )

        group_to_key = {
            'ETL Job run': 'standard',
            'ETL Flex Job Run': 'flex',
            'ETL Memory-Optimized Job Run': 'memory_optimized',
        }

        for item in resp.get('PriceList', []):
            product = json.loads(item) if isinstance(item, str) else item
            group = product.get('product', {}).get('attributes', {}).get('group', '')
            key = group_to_key.get(group)
            if not key:
                continue
            terms = product.get('terms', {}).get('OnDemand', {})
            for term in terms.values():
                for dim in term.get('priceDimensions', {}).values():
                    price = float(dim.get('pricePerUnit', {}).get('USD', 0))
                    if price > 0:
                        rates[key] = price
    except Exception as e:
        logger.warning(f"Failed to fetch Glue pricing, using fallbacks: {e}")

    return rates


def estimate_glue_cost(
    applications: List[Dict[str, Any]],
    region: str = 'us-east-1',
) -> Dict[str, Any]:
    """
    Estimate AWS Glue cost for a list of Spark applications.

    Args:
        applications: List of app dicts with spark_executors, spark_executor_cores,
                      spark_executor_memory_gb, job_runtime_minutes
        region: AWS region for pricing

    Returns:
        Dict with total cost, per-app breakdown, and Glue config details
    """
    dpu_rate = fetch_glue_rates(region)

    total_standard_cost = 0
    total_flex_cost = 0
    app_results = []

    for app in applications:
        executors = app.get('spark_executors', 10)
        cores = app.get('spark_executor_cores', 4)
        mem_gb = app.get('spark_executor_memory_gb', 16)
        runtime_min = app.get('job_runtime_minutes', 120)
        runtime_hrs = runtime_min / 60

        worker_info = calculate_glue_workers(executors, cores, mem_gb, workload_type=app.get('workload_characteristics', 'balanced'))
        # Glue bills for num_workers only — driver is included in the worker count
        total_dpus = worker_info['total_dpus']

        # Pick the right rate based on worker category
        is_memory_optimized = WORKER_TYPES.get(worker_info['worker_type'], {}).get('category') == 'memory_optimized'
        standard_rate = dpu_rate['memory_optimized'] if is_memory_optimized else dpu_rate['standard']
        standard_cost = round(total_dpus * standard_rate * runtime_hrs, 4)
        total_standard_cost += standard_cost

        # Flex pricing (only for G.1X and G.2X)
        flex_eligible = worker_info['worker_type'] in FLEX_ELIGIBLE_WORKERS
        flex_cost = round(total_dpus * dpu_rate['flex'] * runtime_hrs, 4) if flex_eligible else None
        if flex_cost is not None:
            total_flex_cost += flex_cost

        app_results.append({
            'application_id': app.get('application_id', 'unknown'),
            'job_name': app.get('job_name', 'Unknown'),
            'job_runtime_minutes': runtime_min,
            'glue_config': {
                'worker_type': worker_info['worker_type'],
                'worker_specs': worker_info['worker_specs'],
                'num_workers': worker_info['num_workers'],
                'total_dpus': total_dpus,
            },
            'standard_cost': standard_cost,
            'standard_breakdown': f"${standard_cost} = {total_dpus} DPUs × ${standard_rate}/DPU-hr × {runtime_hrs:.2f} hrs",
            'flex_cost': flex_cost,
            'flex_breakdown': f"${flex_cost} = {total_dpus} DPUs × ${dpu_rate['flex']}/DPU-hr × {runtime_hrs:.2f} hrs" if flex_eligible else 'Not available (Flex only supports G.1X and G.2X workers)',
        })

    return {
        'total_standard_cost': round(total_standard_cost, 4),
        'total_flex_cost': round(total_flex_cost, 4) if total_flex_cost > 0 else None,
        'rates': dpu_rate,
        'per_app': app_results,
        'qualitative_benefits': [
            'Visual ETL editor — drag-and-drop job authoring without writing code',
            'Crawlers — auto-discover schemas from S3/databases',
            'Job bookmarks — built-in incremental processing',
            'Built-in monitoring dashboard — no Spark UI setup needed',
            'Fully managed — zero infrastructure to operate',
            'Native integration with Glue Data Quality for automated data validation',
        ],
        'qualitative_limitations': [
            'Fixed Spark versions per Glue release — less flexibility',
            'No local SSDs — shuffle-heavy jobs may be slower',
            'Limited custom Spark config options',
            'No streaming support (use Glue Streaming separately)',
            'DPU pricing can be higher than EC2 Spot for long-running jobs',
            'Flex pricing (34% discount) only available for G.1X and G.2X workers — larger worker types (G.4X+) and memory-optimized (R-series) do not qualify',
            'R-series (memory-optimized) workers cost $0.52/M-DPU-hr vs $0.44/DPU-hr for G-series — 18% premium',
        ],
        'pricing_assumptions': [
            'Estimates assume fixed worker count (no autoscaling) — all workers billed for the full job duration',
            'With Glue autoscaling enabled, actual cost may be lower if the job has phases with lower parallelism',
            'Glue autoscaling can scale workers between 2 and the configured max — savings depend on workload variability',
        ],
    }


def analyze_glue_job(job_name: str, region: str = 'us-east-1', num_runs: int = 3) -> Dict[str, Any]:
    """
    Analyze an existing Glue job's runs and map to Spark executor config
    for cross-platform cost comparison.

    Fetches the most recent successful runs, extracts worker config and runtime,
    and maps Glue workers back to equivalent Spark executor config.

    Args:
        job_name: AWS Glue job name
        region: AWS region
        num_runs: Number of recent runs to analyze (default 3)

    Returns:
        Dict with Spark-equivalent executor config ready for get_emr_pricing
    """
    try:
        glue_client = boto3.client('glue', region_name=region)

        # Get recent job runs
        resp = glue_client.get_job_runs(JobName=job_name, MaxResults=num_runs * 2)
        runs = [r for r in resp.get('JobRuns', []) if r.get('JobRunState') == 'SUCCEEDED'][:num_runs]

        if not runs:
            return {'status': 'error', 'message': f'No successful runs found for Glue job: {job_name}'}

        # Analyze runs
        analyzed_runs = []
        for run in runs:
            worker_type = run.get('WorkerType', 'G.1X')
            num_workers = run.get('NumberOfWorkers', 2)
            dpu_seconds = run.get('DPUSeconds', 0)
            execution_time = run.get('ExecutionTime', 0)  # seconds
            run_id = run.get('Id', '')

            # Map Glue worker to Spark executor config
            spec = WORKER_TYPES.get(worker_type, WORKER_TYPES['G.1X'])
            # In Glue, 1 worker = 1 executor. One worker is the driver.
            spark_executors = max(1, num_workers - 1)  # subtract driver
            spark_cores = spec['vcpus']
            spark_memory_gb = spec['memory_gb']

            # Actual Glue cost from DPU-seconds
            is_memory = spec.get('category') == 'memory_optimized'
            rate = 0.52 if is_memory else 0.44
            actual_glue_cost = round((dpu_seconds / 3600) * rate, 4) if dpu_seconds else None

            analyzed_runs.append({
                'run_id': run_id,
                'glue_config': {
                    'worker_type': worker_type,
                    'num_workers': num_workers,
                    'dpu_seconds': dpu_seconds,
                    'execution_time_seconds': execution_time,
                    'execution_time_minutes': round(execution_time / 60, 2),
                },
                'actual_glue_cost': actual_glue_cost,
                'spark_equivalent': {
                    'spark_executors': spark_executors,
                    'spark_executor_cores': spark_cores,
                    'spark_executor_memory_gb': spark_memory_gb,
                    'job_runtime_minutes': round(execution_time / 60, 2),
                    'job_name': job_name,
                    'application_id': f'glue_{run_id[:16]}',
                    'workload_characteristics': 'batch_processing',
                },
            })

        # Use the most recent run as the primary for pricing
        primary = analyzed_runs[0]

        return {
            'status': 'success',
            'job_name': job_name,
            'runs_analyzed': len(analyzed_runs),
            'primary_run': primary,
            'all_runs': analyzed_runs,
            'spark_applications': [r['spark_equivalent'] for r in analyzed_runs[:1]],
            'actual_glue_cost': primary['actual_glue_cost'],
            'note': f'Mapped Glue {primary["glue_config"]["worker_type"]} × {primary["glue_config"]["num_workers"]} workers to {primary["spark_equivalent"]["spark_executors"]} Spark executors × {primary["spark_equivalent"]["spark_executor_cores"]} cores × {primary["spark_equivalent"]["spark_executor_memory_gb"]}GB',
        }

    except Exception as e:
        return {'status': 'error', 'message': f'Failed to analyze Glue job: {str(e)}'}

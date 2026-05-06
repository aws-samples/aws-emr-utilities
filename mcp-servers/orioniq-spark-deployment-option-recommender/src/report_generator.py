"""
OrionIQ Report Generator

Generates a deterministic HTML report from structured pricing data.
The agent calls generate_report() — no formatting decisions by the LLM.
"""

import os
import json
from datetime import datetime
from typing import Dict, Any, Optional


def generate_report(
    pricing_result: Dict[str, Any],
    workload_description: str = '',
    region: str = 'us-east-1',
    has_kubernetes_experience: bool = False,
    output_dir: str = 'output',
) -> Dict[str, Any]:
    """
    Generate a deterministic HTML report from get_emr_pricing output.

    Args:
        pricing_result: Full output from get_emr_pricing
        workload_description: User's workload description
        region: AWS region
        has_kubernetes_experience: K8s experience flag
        output_dir: Directory to write the report

    Returns:
        Dict with file path and status
    """
    os.makedirs(output_dir, exist_ok=True)
    timestamp = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
    filename = f'OrionIQ_Report_{timestamp}.html'
    filepath = os.path.join(output_dir, filename)

    # Extract data
    apps = pricing_result.get('individual_workload_analysis', [])
    cluster_ctx = pricing_result.get('cluster_context', {})
    rates = pricing_result.get('pricing_rates', {})
    recommendation = pricing_result.get('recommendation', {})
    glue = pricing_result.get('aws_glue_estimate')
    ranked = recommendation.get('ranked_options', [])

    # Build cost rows
    cost_rows = ''
    for i, opt in enumerate(ranked):
        badge = ' ✅' if i == 0 else ''
        style = 'font-weight:bold;background:#e8f5e9;' if i == 0 else ''
        cost_rows += f'<tr style="{style}"><td>{opt["option"]}{badge}</td><td>${opt["total_cost"]:.4f}</td><td>{opt["score"]}</td>'
        evidence = '; '.join(opt.get('evidence', [])[:2]) or '—'
        cost_rows += f'<td>{evidence}</td></tr>\n'

    # Glue rows
    glue_rows = ''
    if glue and glue.get('per_app'):
        std = glue.get('total_standard_cost', 0)
        flex = glue.get('total_flex_cost')
        glue_rows = f'<tr><td>AWS Glue (Standard)</td><td>${std:.4f}</td><td>—</td><td>Managed experience with premium pricing</td></tr>\n'
        if flex:
            glue_rows += f'<tr><td>AWS Glue (Flex)</td><td>${flex:.4f}</td><td>—</td><td>34% discount (G.1X/G.2X only)</td></tr>\n'

    # App detail rows
    app_rows = ''
    for app in apps:
        app_rows += f'<tr><td>{app.get("job_name", "Unknown")}</td><td>{app.get("job_runtime_minutes", 0):.2f} min</td>'
        costs = app.get('cost_analysis', {})
        for key in ['EMR Serverless', 'EMR on EC2', 'EMR on EC2 (Spot)', 'EMR on EKS', 'EMR on EKS (Spot)']:
            val = costs.get(key, '—')
            if isinstance(val, str) and val.startswith('$'):
                val = val.split('=')[0].strip()
            app_rows += f'<td>{val}</td>'
        app_rows += '</tr>\n'

    # Glue per-app rows
    glue_app_rows = ''
    if glue and glue.get('per_app'):
        for ga in glue['per_app']:
            cfg = ga.get('glue_config', {})
            glue_app_rows += f'<tr><td>{ga.get("job_name","")}</td><td>{cfg.get("worker_type","")}</td>'
            glue_app_rows += f'<td>{cfg.get("num_workers","")}</td><td>{cfg.get("total_dpus","")}</td>'
            glue_app_rows += f'<td>${ga.get("standard_cost",0):.4f}</td>'
            flex_val = f'${ga["flex_cost"]:.4f}' if ga.get("flex_cost") is not None else 'N/A'
            glue_app_rows += f'<td>{flex_val}</td></tr>\n'

    # Pricing rates rows
    rate_rows = ''
    for itype, rate in rates.get('ec2_rates', {}).items():
        emr = rates.get('emr_rates', {}).get(itype, 0)
        spot = rates.get('spot_rates', {}).get(itype, 'N/A')
        spot_str = f'${spot:.4f}' if isinstance(spot, (int, float)) else spot
        rate_rows += f'<tr><td>{itype}</td><td>${rate:.4f}</td><td>${emr:.4f}</td><td>{spot_str}</td></tr>\n'

    # Caveats
    caveats = []
    if pricing_result.get('data_quality') != 'measured':
        caveats.append('Some metrics use estimated defaults — not real cluster/Spark data.')
    if cluster_ctx.get('estimated_from_spark_data'):
        caveats.append(f'Cluster config estimated from Spark data: {cluster_ctx.get("estimated_instance_count")}× {cluster_ctx.get("estimated_instance_type")}')
    caveats.append('Spot prices are a snapshot — actual costs fluctuate. Spot instances can be interrupted with 2-minute notice.')
    if glue:
        caveats.append('Glue estimates assume fixed worker count (no autoscaling). With autoscaling, actual cost may be lower.')
    caveats.append('For precise future cost projections, use the AWS Pricing Calculator (https://calculator.aws/).')
    caveat_html = '\n'.join(f'<li>{c}</li>' for c in caveats)

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>OrionIQ Deployment Recommendation Report</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; max-width: 1000px; margin: 0 auto; padding: 20px; color: #333; }}
  h1 {{ color: #232f3e; border-bottom: 3px solid #ff9900; padding-bottom: 10px; }}
  h2 {{ color: #232f3e; margin-top: 30px; }}
  table {{ border-collapse: collapse; width: 100%; margin: 15px 0; }}
  th, td {{ border: 1px solid #ddd; padding: 8px 12px; text-align: left; }}
  th {{ background: #232f3e; color: white; }}
  tr:nth-child(even) {{ background: #f9f9f9; }}
  .badge {{ display: inline-block; background: #ff9900; color: white; padding: 2px 8px; border-radius: 4px; font-size: 12px; }}
  .meta {{ color: #666; font-size: 14px; }}
  .caveat {{ background: #fff3cd; border-left: 4px solid #ffc107; padding: 10px 15px; margin: 10px 0; }}
  .summary {{ background: #e8f5e9; border-left: 4px solid #4caf50; padding: 15px; margin: 15px 0; font-size: 16px; }}
  .footer {{ margin-top: 40px; padding-top: 15px; border-top: 1px solid #ddd; color: #999; font-size: 12px; }}
</style>
</head>
<body>

<h1>🚀 OrionIQ Deployment Recommendation Report</h1>
<p class="meta">Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')} | Region: {region} | Kubernetes experience: {'Yes' if has_kubernetes_experience else 'No'}</p>

<div class="summary">
<strong>Recommendation: {recommendation.get('primary_recommendation', 'N/A')}</strong>
(Score: {recommendation.get('confidence_score', 'N/A')}) —
{recommendation.get('key_insight', '')}
</div>

<h2>1. Workload Profile</h2>
<table>
<tr><th>Attribute</th><th>Value</th></tr>
<tr><td>Workload</td><td>{workload_description or 'N/A'}</td></tr>
<tr><td>Cluster Runtime</td><td>{cluster_ctx.get('cluster_runtime_hours', 'N/A')} hours</td></tr>
<tr><td>Utilization</td><td>{cluster_ctx.get('utilization_percentage', 'N/A')}%</td></tr>
<tr><td>Spot Usage</td><td>{cluster_ctx.get('spot_instance_percentage', 0)*100:.0f}%</td></tr>
<tr><td>Applications</td><td>{len(apps)}</td></tr>
</table>

<h2>2. Cost Comparison</h2>
<table>
<tr><th>Deployment Option</th><th>Estimated Cost</th><th>Score</th><th>Key Evidence</th></tr>
{cost_rows}{glue_rows}</table>

<h2>3. Per-Application Breakdown</h2>
<table>
<tr><th>Application</th><th>Runtime</th><th>Serverless</th><th>EC2 OD</th><th>EC2 Spot</th><th>EKS OD</th><th>EKS Spot</th></tr>
{app_rows}</table>

{'<h2>4. AWS Glue Breakdown</h2><table><tr><th>Application</th><th>Worker Type</th><th>Workers</th><th>DPUs</th><th>Standard</th><th>Flex</th></tr>' + glue_app_rows + '</table>' if glue_app_rows else ''}

<h2>{'5' if glue_app_rows else '4'}. Pricing Rates Used</h2>
<table>
<tr><th>Instance Type</th><th>EC2 On-Demand</th><th>EMR Uplift</th><th>Spot Price</th></tr>
{rate_rows}</table>
<p>EMR Serverless: ${rates.get('emr_serverless', {}).get('vcpu_hour_rate', 'N/A')}/vCPU-hr + ${rates.get('emr_serverless', {}).get('memory_gb_hour_rate', 'N/A')}/GB-hr</p>
<p>EKS Cluster: ${rates.get('eks_cluster', {}).get('hourly_rate', 'N/A')}/hr | EMR on EKS: ${rates.get('emr_eks', {}).get('vcpu_hour_rate', 'N/A')}/vCPU-hr + ${rates.get('emr_eks', {}).get('memory_gb_hour_rate', 'N/A')}/GB-hr</p>

<h2>{'6' if glue_app_rows else '5'}. Caveats & Assumptions</h2>
<div class="caveat"><ul>{caveat_html}</ul></div>

<div class="footer">
Generated by OrionIQ — AI-Powered Data Platform Advisor | github.com/aws-samples/aws-emr-utilities
</div>

</body>
</html>"""

    with open(filepath, 'w') as f:
        f.write(html)

    return {
        'status': 'success',
        'filepath': filepath,
        'filename': filename,
    }

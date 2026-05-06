#!/usr/bin/env python3
"""
OrionIQ CLI — Standalone deployment recommender. No LLM required.

Usage:
  # Analyze an EMR cluster
  uv run python3 orioniq_cli.py --cluster j-XXXXX --region us-east-1

  # Analyze a Spark app from History Server
  uv run python3 orioniq_cli.py --app application_XXXX_YYYY --region us-east-1

  # Include Glue in comparison
  uv run python3 orioniq_cli.py --cluster j-XXXXX --region us-east-1 --include-glue

  # With Kubernetes experience
  uv run python3 orioniq_cli.py --cluster j-XXXXX --region us-east-1 --k8s-experience

  # Generate HTML report
  uv run python3 orioniq_cli.py --cluster j-XXXXX --region us-east-1 --report
"""

import argparse
import json
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))
import combined_emr_advisor_mcp as orioniq


def run(args):
    # Set workflow state
    orioniq._workflow_state['plan_generated'] = True
    orioniq._workflow_state['context_collected'] = True
    orioniq._workflow_data['has_kubernetes_experience'] = args.k8s_experience
    desc = f"{'include glue ' if args.include_glue else ''}cost optimization"
    orioniq._workflow_data['workload_description'] = desc

    # Step 1: Analyze cluster or use defaults
    if args.cluster:
        print(f"📊 Analyzing cluster {args.cluster} in {args.region}...")
        result = orioniq.analyze_cluster_patterns(cluster_ids=[args.cluster], region=args.region)
        apps = result.get('applications_found', 0)
        idle = result.get('pattern_analysis', {}).get('idle_time_percentage', '?')
        print(f"   Spark metrics available: {result.get('spark_metrics_available', False)}")
        print(f"   Idle time: {idle}%")
    else:
        orioniq.analyze_cluster_patterns(region=args.region)

    # Step 2: Analyze Spark config
    print("⚡ Analyzing Spark job configuration...")
    spark = orioniq.analyze_spark_job_config()
    source = spark.get('data_source', 'defaults')
    print(f"   Data source: {source}")

    # Step 3: Calculate pricing
    print("💰 Calculating pricing across all deployment options...")
    pricing = orioniq.get_emr_pricing(region=args.region)

    if pricing.get('status') != 'success':
        print(f"❌ Error: {pricing.get('message')}")
        sys.exit(1)

    # Display results
    rec = pricing.get('recommendation', {})
    ranked = rec.get('ranked_options', [])

    print(f"\n{'='*65}")
    print(f"  OrionIQ Deployment Recommendation")
    print(f"  Region: {args.region} | K8s: {'Yes' if args.k8s_experience else 'No'}")
    print(f"{'='*65}")

    print(f"\n  🏆 Recommendation: {rec.get('primary_recommendation', 'N/A')}")
    print(f"     Score: {rec.get('confidence_score', 'N/A')}")
    print()

    # Cost table
    print(f"  {'Option':<28} {'Cost':>10} {'Score':>7}")
    print(f"  {'-'*28} {'-'*10} {'-'*7}")
    for opt in ranked:
        marker = ' ✅' if opt == ranked[0] else ''
        print(f"  {opt['option']:<28} ${opt['total_cost']:>9.4f} {opt['score']:>6.1f}{marker}")

    # Glue
    glue = pricing.get('aws_glue_estimate')
    if glue and glue.get('total_standard_cost'):
        print(f"  {'AWS Glue (Standard)':<28} ${glue['total_standard_cost']:>9.4f}     —")
        if glue.get('total_flex_cost'):
            print(f"  {'AWS Glue (Flex)':<28} ${glue['total_flex_cost']:>9.4f}     —")

    # Per-app
    apps = pricing.get('individual_workload_analysis', [])
    if apps:
        print(f"\n  Per-Application:")
        for app in apps:
            print(f"    {app.get('job_name', 'Unknown')} ({app.get('job_runtime_minutes', 0):.1f} min)")

    # Evidence
    if ranked:
        print(f"\n  Evidence:")
        for e in ranked[0].get('evidence', [])[:3]:
            print(f"    • {e}")

    # Data quality
    if pricing.get('data_quality') != 'measured':
        print(f"\n  ⚠️  {pricing.get('data_quality_warning', 'Some estimates used defaults')}")

    # Report
    if args.report:
        print(f"\n📄 Generating report...")
        report = orioniq.generate_report(region=args.region)
        if report.get('status') == 'success':
            print(f"   Saved: {report['filepath']}")
        else:
            print(f"   ❌ {report.get('message')}")

    print()


def main():
    parser = argparse.ArgumentParser(
        description='OrionIQ — AI-Powered Data Platform Advisor (No LLM required)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument('--cluster', '-c', help='EMR cluster ID (e.g., j-XXXXX)')
    parser.add_argument('--app', '-a', help='Spark application ID')
    parser.add_argument('--region', '-r', default='us-east-1', help='AWS region (default: us-east-1)')
    parser.add_argument('--k8s-experience', action='store_true', help='Team has Kubernetes experience')
    parser.add_argument('--include-glue', action='store_true', help='Include AWS Glue in comparison')
    parser.add_argument('--report', action='store_true', help='Generate HTML report')
    parser.add_argument('--json', action='store_true', help='Output raw JSON')

    args = parser.parse_args()

    if not args.cluster and not args.app:
        parser.error('Provide --cluster or --app')

    if args.json:
        # Minimal output for scripting
        orioniq._workflow_state['plan_generated'] = True
        orioniq._workflow_state['context_collected'] = True
        orioniq._workflow_data['has_kubernetes_experience'] = args.k8s_experience
        orioniq._workflow_data['workload_description'] = 'include glue' if args.include_glue else ''
        if args.cluster:
            orioniq.analyze_cluster_patterns(cluster_ids=[args.cluster], region=args.region)
        else:
            orioniq.analyze_cluster_patterns(region=args.region)
        orioniq.analyze_spark_job_config()
        result = orioniq.get_emr_pricing(region=args.region)
        print(json.dumps(result, indent=2, default=str))
    else:
        run(args)


if __name__ == '__main__':
    main()

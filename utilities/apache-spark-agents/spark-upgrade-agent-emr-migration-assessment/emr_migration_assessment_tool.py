#!/usr/bin/env python3
"""
EMR Migration Assessment Tool 

Combines EMR assessment, lifecycle tracking, and dashboard generation.
Usage: python emr_migration_tool.py --profile <aws_profile> --region <region>
"""

import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional

import boto3
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import requests
from bs4 import BeautifulSoup
from plotly.subplots import make_subplots


# ============================================================================
# EMR LIFECYCLE MODULE
# ============================================================================

# Static lookup table for EMR versions <= 7.2.0
STATIC_LIFECYCLE_DATA = {
    'emr-7.2.0': {'eos': '2026-07-25', 'eol': '2027-07-25'},
    'emr-7.1.0': {'eos': '2026-05-01', 'eol': '2027-05-01'},
    'emr-7.0.0': {'eos': '2026-05-01', 'eol': '2027-05-01'},
    'emr-6.15.0': {'eos': '2026-05-01', 'eol': '2027-05-01'},
    'emr-6.14.0': {'eos': '2026-05-01', 'eol': '2027-05-01'},
    'emr-6.13.0': {'eos': '2026-05-01', 'eol': '2027-05-01'},
    'emr-6.12.0': {'eos': '2026-05-01', 'eol': '2027-05-01'},
    'emr-6.11.0': {'eos': '2026-05-01', 'eol': '2027-05-01'},
    'emr-6.10.0': {'eos': '2026-05-01', 'eol': '2027-05-01'},
    'emr-6.9.0': {'eos': '2026-05-01', 'eol': '2027-05-01'},
    'emr-6.8.0': {'eos': '2026-05-01', 'eol': '2027-05-01'},
    'emr-6.7.0': {'eos': '2026-05-01', 'eol': '2027-05-01'},
    'emr-6.6.0': {'eos': '2026-05-01', 'eol': '2027-05-01'},
    'emr-6.5.0': {'eos': '2026-05-01', 'eol': '2027-05-01'},
    'emr-6.4.0': {'eos': '2026-05-01', 'eol': '2027-05-01'},
    'emr-6.3.0': {'eos': '2026-05-01', 'eol': '2027-05-01'},
    'emr-6.2.0': {'eos': '2026-05-01', 'eol': '2027-05-01'},
    'emr-6.1.0': {'eos': '2026-05-01', 'eol': '2027-05-01'},
    'emr-6.0.0': {'eos': '2026-05-01', 'eol': '2027-05-01'},
    'emr-5.36.0': {'eos': '2026-05-01', 'eol': '2027-05-01'},
}


def parse_emr_version(emr_version: str) -> tuple:
    """Parse EMR version string into (major, minor, patch) tuple."""
    version_str = emr_version.replace('emr-', '')
    parts = version_str.split('.')
    major = int(parts[0]) if len(parts) > 0 and parts[0].isdigit() else 0
    minor = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    patch = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
    return (major, minor, patch)


def get_doc_url(emr_version: str) -> str:
    """Generate AWS documentation URL for EMR version."""
    version_str = emr_version.replace('emr-', '').replace('.', '')
    return f"https://docs.aws.amazon.com/emr/latest/ReleaseGuide/emr-{version_str}-release.html"


def parse_date(date_str: str) -> Optional[str]:
    """Parse various date formats to YYYY-MM-DD format."""
    if not date_str:
        return None
    date_str = date_str.strip()
    formats = ['%B %d, %Y', '%b %d, %Y', '%m/%d/%Y', '%Y-%m-%d', '%d %B %Y', '%d %b %Y']
    for fmt in formats:
        try:
            dt = datetime.strptime(date_str, fmt)
            return dt.strftime('%Y-%m-%d')
        except ValueError:
            continue
    return None


def scrape_lifecycle_from_docs(emr_version: str) -> Optional[Dict[str, str]]:
    """Scrape EOS and EOL dates from AWS EMR documentation."""
    url = get_doc_url(emr_version)
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        tables = soup.find_all('table')
        
        for table in tables:
            headers = table.find_all('th')
            header_text = ' '.join([h.get_text().strip().lower() for h in headers])
            
            # Check for vertical table format (Support phase / Date)
            if 'support phase' in header_text or 'date' in header_text:
                rows = table.find_all('tr')
                eos_date = None
                eol_date = None
                
                for row in rows[1:]:
                    cells = row.find_all('td')
                    if len(cells) >= 2:
                        phase = cells[0].get_text().strip().lower()
                        date_text = cells[1].get_text().strip()
                        
                        if 'end of support' in phase:
                            eos_date = parse_date(date_text)
                        elif 'end of life' in phase:
                            eol_date = parse_date(date_text)
                
                if eos_date and eol_date:
                    return {'eos': eos_date, 'eol': eol_date}
            
            # Check for horizontal table format (Release label / EOS / EOL)
            if 'end of support' in header_text or 'end of life' in header_text:
                rows = table.find_all('tr')
                for row in rows[1:]:
                    cells = row.find_all('td')
                    if len(cells) >= 3:
                        version_cell = cells[0].get_text().strip()
                        if emr_version in version_cell or emr_version.replace('emr-', '') in version_cell:
                            eos_date = None
                            eol_date = None
                            for i, cell in enumerate(cells):
                                cell_text = cell.get_text().strip()
                                header = headers[i].get_text().strip().lower() if i < len(headers) else ''
                                if 'end of support' in header:
                                    eos_date = parse_date(cell_text)
                                elif 'end of life' in header:
                                    eol_date = parse_date(cell_text)
                            if eos_date and eol_date:
                                return {'eos': eos_date, 'eol': eol_date}
        
        raise Exception("No support phase table found in the public release doc!")
    except Exception as e:
        raise Exception(f"Error: Could not scrape lifecycle dates for {emr_version}: {e}")



def get_emr_lifecycle_dates(emr_version: str) -> Dict[str, str]:
    """Get End of Support and End of Life dates for an EMR version."""
    if not emr_version.startswith('emr-'):
        emr_version = f'emr-{emr_version}'
    
    if emr_version in STATIC_LIFECYCLE_DATA:
        return STATIC_LIFECYCLE_DATA[emr_version]
    
    try:
        major, minor, patch = parse_emr_version(emr_version)
        if major < 7 or (major == 7 and minor <= 2):
            if major == 7 and minor == 2:
                return {'eos': '2026-07-25', 'eol': '2027-07-25'}
            else:
                return {'eos': '2026-05-01', 'eol': '2027-05-01'}
        
        if major >= 7 and minor >= 3:
            scraped_data = scrape_lifecycle_from_docs(emr_version)
            return scraped_data
    except Exception as e:
        raise Exception(f"Error processing version {emr_version}: {e}")
    

def calculate_days_to_lifecycle(emr_version: str) -> Dict:
    """Calculate days remaining to End of Support and End of Life."""
    dates = get_emr_lifecycle_dates(emr_version)
    today = datetime.now()
    eos_date = datetime.strptime(dates['eos'], '%Y-%m-%d')
    eol_date = datetime.strptime(dates['eol'], '%Y-%m-%d')
    days_to_eos = (eos_date - today).days
    days_to_eol = (eol_date - today).days
    return {
        'days_to_eos': days_to_eos,
        'days_to_eol': days_to_eol,
        'eos_date': dates['eos'],
        'eol_date': dates['eol']
    }



# ============================================================================
# EMR ASSESSMENT MODULE
# ============================================================================

def assess_emr_upgrades(region='us-east-1', profile=None):
    """Assess EMR clusters and extract application usage data."""
    session = boto3.Session(profile_name=profile, region_name=region) if profile else boto3.Session(region_name=region)
    emr = session.client('emr')
    
    one_month_ago = datetime.now() - timedelta(days=30)
    clusters = emr.list_clusters(ClusterStates=['RUNNING', 'WAITING'])['Clusters']
    
    results = defaultdict(lambda: {'spark_versions': set(), 'count': 0, 'last_run': None, 'cluster_ids': set()})
    
    print(f"Found {len(clusters)} clusters in {region}")
    
    for cluster in clusters:
        cluster_id = cluster['Id']
        try:
            cluster_detail = emr.describe_cluster(ClusterId=cluster_id)['Cluster']
            emr_version = cluster_detail['ReleaseLabel']
            spark_version = next((app['Version'] for app in cluster_detail.get('Applications', [])
                                  if app['Name'] == 'Spark'), 'Unknown')
            
            paginator = emr.get_paginator('list_steps')
            for page in paginator.paginate(ClusterId=cluster_id):
                for step in page['Steps']:
                    step_time = step['Status']['Timeline'].get('EndDateTime') or step['Status']['Timeline'].get('StartDateTime')
                    
                    if not step_time or step_time.replace(tzinfo=None) < one_month_ago:
                        continue
                    
                    args = step.get('Config', {}).get('Args', [])
                    
                    if 'spark-submit' in args:
                        remaining = args[args.index('spark-submit')+1:]
                        script = None
                        i = 0
                        while i < len(remaining):
                            if remaining[i].startswith('-'):
                                i += 2
                            else:
                                script = remaining[i]
                                break
                        
                        if script:
                            key = (script, emr_version)
                            results[key]['spark_versions'].add(spark_version)
                            results[key]['count'] += 1
                            results[key]['cluster_ids'].add(cluster_id)
                            
                            if not results[key]['last_run'] or step_time > results[key]['last_run']:
                                results[key]['last_run'] = step_time
        except Exception as e:
            print(f"Error processing cluster {cluster_id}: {e}")
            continue
    
    output = []
    for (script, emr_ver), data in results.items():
        output.append({
            'Application': script,
            'Spark Version': ', '.join(sorted(data['spark_versions'])),
            'EMR Version': emr_ver,
            'Step Count': data['count'],
            'Last Run': data['last_run'].strftime('%Y-%m-%d %H:%M') if data['last_run'] else 'N/A',
            'Cluster IDs': ', '.join(list(data['cluster_ids'])[:3]),
            'Region': region
        })
    
    return sorted(output, key=lambda x: (x['EMR Version'], -x['Step Count']))



# ============================================================================
# DASHBOARD GENERATION MODULE
# ============================================================================

def extract_app_name(s3_path):
    """Extract filename from S3 path."""
    if isinstance(s3_path, str):
        if s3_path.startswith('s3://'):
            return s3_path.split('/')[-1]
        elif s3_path.startswith('/'):
            return s3_path.split('/')[-1]
        return s3_path
    return str(s3_path)


def get_spark_color(spark_version):
    """Get color based on Spark version."""
    try:
        version = float('.'.join(str(spark_version).split('.')[:2]))
        if version < 3.5:
            return 'red'
        else:
            return 'teal'
    except:
        return 'gray'


def get_emr_color(emr_version):
    """Get color based on EMR version - Red for 5.x, Orange for 6.x, Green for 7.x."""
    emr_str = str(emr_version)
    if emr_str.startswith('emr-5'):
        return '#d62728'
    elif emr_str.startswith('emr-6'):
        return '#ff7f0e'
    elif emr_str.startswith('emr-7'):
        return '#2ca02c'
    else:
        return '#7f7f7f'


def sort_version_key(version):
    """Create sortable key for version strings."""
    try:
        version_str = str(version)
        if version_str.startswith('emr-'):
            parts = version_str[4:].split('.')
        else:
            parts = version_str.split('.')
        major = int(parts[0]) if parts[0].isdigit() else 0
        minor = int(parts[1]) / 100 if len(parts) > 1 and parts[1].isdigit() else 0
        patch = int(parts[2]) / 10000 if len(parts) > 2 and parts[2].isdigit() else 0
        return major + minor + patch
    except:
        return 0



def create_dashboard(apps, output_file='emr_migration_dashboard.html'):
    """Create interactive EMR migration dashboard."""
    if not apps:
        print("No application data found!")
        return None
    
    df = pd.DataFrame(apps)
    print(f"DataFrame shape: {df.shape}")
    
    df['App Name'] = df['Application'].apply(extract_app_name)
    df['Spark Color'] = df['Spark Version'].apply(get_spark_color)
    df['EMR Color'] = df['EMR Version'].apply(get_emr_color)
    df['EMR Sort Key'] = df['EMR Version'].apply(sort_version_key)
    
    lifecycle_data = df['EMR Version'].apply(calculate_days_to_lifecycle)
    df['Days to EOS'] = lifecycle_data.apply(lambda x: x['days_to_eos'])
    df['Days to EOL'] = lifecycle_data.apply(lambda x: x['days_to_eol'])
    df['EOS Date'] = lifecycle_data.apply(lambda x: x['eos_date'])
    df['EOL Date'] = lifecycle_data.apply(lambda x: x['eol_date'])
    
    df_grouped = df.groupby(['App Name', 'EMR Version']).agg({
        'Step Count': 'sum',
        'Spark Version': 'first',
        'Last Run': 'max',
        'Days to EOS': 'first',
        'Days to EOL': 'first',
        'EOS Date': 'first',
        'EOL Date': 'first',
        'EMR Sort Key': 'first'
    }).reset_index()
    
    df_grouped['Spark Color'] = df_grouped['Spark Version'].apply(get_spark_color)
    df_grouped['EMR Color'] = df_grouped['EMR Version'].apply(get_emr_color)
    
    fig = make_subplots(
        rows=6, cols=1,
        subplot_titles=[
            'Migration Priority Matrix (Usage vs EMR Version)',
            'Applications by EMR Version (Sorted by Usage)',
            'Top 10 Applications by Usage & EMR Version',
            'EMR Version Lifecycle Summary',
            'Top 15 Apps: Days to End of Support',
            'Top 15 Apps: Days to End of Life'
        ],
        specs=[[{"type": "scatter"}],
               [{"type": "bar"}],
               [{"type": "bar"}],
               [{"type": "bar"}],
               [{"type": "bar"}],
               [{"type": "bar"}]],
        vertical_spacing=0.05
    )
    
    # Graph 1: Migration Priority Matrix
    fig.add_trace(
        go.Scatter(
            x=df_grouped['Step Count'],
            y=df_grouped['EMR Sort Key'],
            mode='markers',
            marker=dict(
                size=[max(5, min(50, x * 8)) for x in df_grouped['Step Count']],
                color=df_grouped['Spark Color'],
                opacity=0.7,
                line=dict(width=1, color='black')
            ),
            text=df_grouped.apply(lambda x: f"App: {x['App Name']}<br>EMR: {x['EMR Version']}<br>Spark: {x['Spark Version']}<br>Last Run: {x['Last Run']}<br>Steps: {x['Step Count']}", axis=1),
            hovertemplate='%{text}<extra></extra>',
            name='Applications',
            showlegend=False
        ),
        row=1, col=1
    )

    
    # Graph 2: Applications by EMR Version
    emr_usage = df_grouped.groupby('EMR Version').agg({
        'Step Count': 'sum',
        'App Name': 'count'
    }).reset_index()
    emr_usage.columns = ['EMR Version', 'Total Steps', 'App Count']
    emr_usage = emr_usage.sort_values('Total Steps', ascending=True)
    emr_colors_grouped = [get_emr_color(v) for v in emr_usage['EMR Version']]
    
    fig.add_trace(
        go.Bar(
            x=emr_usage['Total Steps'],
            y=emr_usage['EMR Version'],
            orientation='h',
            marker_color=emr_colors_grouped,
            text=[f"{steps}" for steps in emr_usage['Total Steps']],
            textposition='auto',
            name='EMR Usage',
            showlegend=False,
            hovertemplate='<b>%{y}</b><br>Total Steps: %{x}<br>Applications: %{customdata}<extra></extra>',
            customdata=emr_usage['App Count']
        ),
        row=2, col=1
    )
    
    # Graph 3: Top 10 Applications
    top_10_apps = df_grouped.nlargest(10, 'Step Count').copy()
    top_10_apps = top_10_apps.sort_values('Step Count', ascending=True)
    top_10_apps['Display Name'] = top_10_apps['App Name'] + ' (' + top_10_apps['EMR Version'] + ')'
    app_colors = [get_emr_color(v) for v in top_10_apps['EMR Version']]
    
    fig.add_trace(
        go.Bar(
            x=top_10_apps['Step Count'],
            y=top_10_apps['Display Name'],
            orientation='h',
            marker_color=app_colors,
            text=[f"{steps}" for steps in top_10_apps['Step Count']],
            textposition='auto',
            name='Top Apps',
            showlegend=False,
            hovertemplate='<b>%{customdata[0]}</b><br>Steps: %{x}<br>EMR: %{customdata[1]}<br>Spark: %{customdata[2]}<extra></extra>',
            customdata=top_10_apps[['App Name', 'EMR Version', 'Spark Version']].values
        ),
        row=3, col=1
    )
    
    # Graph 4: EMR Version Lifecycle Summary
    lifecycle_summary = df_grouped.groupby(['EMR Version', 'EOS Date', 'EOL Date']).agg({
        'App Name': 'count',
        'Step Count': 'sum'
    }).reset_index()
    lifecycle_summary.columns = ['EMR Version', 'EOS Date', 'EOL Date', 'App Count', 'Total Steps']
    lifecycle_summary = lifecycle_summary.sort_values('EMR Version', key=lambda x: x.map(sort_version_key))
    
    fig.add_trace(
        go.Bar(
            x=lifecycle_summary['EMR Version'],
            y=lifecycle_summary['App Count'],
            name='App Count',
            marker_color='lightblue',
            text=lifecycle_summary['App Count'],
            textposition='auto',
            hovertemplate='<b>%{x}</b><br>Applications: %{y}<br>EOS: %{customdata[0]}<br>EOL: %{customdata[1]}<extra></extra>',
            customdata=lifecycle_summary[['EOS Date', 'EOL Date']].values
        ),
        row=4, col=1
    )

    
    # Graph 5: Days to End of Support
    top_apps_lifecycle = df_grouped.nlargest(15, 'Step Count').copy()
    top_apps_lifecycle = top_apps_lifecycle.sort_values('Days to EOS', ascending=True)
    top_apps_lifecycle['Display Name'] = top_apps_lifecycle['App Name'] + ' (' + top_apps_lifecycle['EMR Version'] + ')'
    eos_colors = ['#d62728' if days < 365 else '#ff7f0e' if days < 730 else '#2ca02c' 
                  for days in top_apps_lifecycle['Days to EOS']]
    
    fig.add_trace(
        go.Bar(
            x=top_apps_lifecycle['Days to EOS'],
            y=top_apps_lifecycle['Display Name'],
            orientation='h',
            marker_color=eos_colors,
            text=[f"{days}d" for days in top_apps_lifecycle['Days to EOS']],
            textposition='inside',
            textfont=dict(color='white', size=10),
            name='Days to EOS',
            showlegend=False,
            hovertemplate='<b>%{customdata[0]}</b><br>Days to EOS: %{x}<br>EOS Date: %{customdata[1]}<br>EMR: %{customdata[2]}<br>Steps: %{customdata[3]}<extra></extra>',
            customdata=top_apps_lifecycle[['App Name', 'EOS Date', 'EMR Version', 'Step Count']].values
        ),
        row=5, col=1
    )
    
    # Graph 6: Days to End of Life
    top_apps_lifecycle_eol = top_apps_lifecycle.sort_values('Days to EOL', ascending=True)
    eol_colors = ['#d62728' if days < 365 else '#ff7f0e' if days < 730 else '#2ca02c' 
                  for days in top_apps_lifecycle_eol['Days to EOL']]
    
    fig.add_trace(
        go.Bar(
            x=top_apps_lifecycle_eol['Days to EOL'],
            y=top_apps_lifecycle_eol['Display Name'],
            orientation='h',
            marker_color=eol_colors,
            text=[f"{days}d" for days in top_apps_lifecycle_eol['Days to EOL']],
            textposition='inside',
            textfont=dict(color='white', size=10),
            name='Days to EOL',
            showlegend=False,
            hovertemplate='<b>%{customdata[0]}</b><br>Days to EOL: %{x}<br>EOL Date: %{customdata[1]}<br>EMR: %{customdata[2]}<br>Steps: %{customdata[3]}<extra></extra>',
            customdata=top_apps_lifecycle_eol[['App Name', 'EOL Date', 'EMR Version', 'Step Count']].values
        ),
        row=6, col=1
    )
    
    fig.update_layout(
        title_text="EMR Migration Dashboard - Priority Analysis",
        title_x=0.5,
        height=2400,
        showlegend=True,
        font=dict(size=11)
    )
    
    fig.update_xaxes(title_text="Step Count (Usage Frequency)", row=1, col=1)
    fig.update_yaxes(title_text="EMR Version (Numeric)", row=1, col=1)
    fig.update_xaxes(title_text="Total Steps", row=2, col=1)
    fig.update_yaxes(title_text="EMR Version", row=2, col=1)
    fig.update_xaxes(title_text="Step Count", row=3, col=1)
    fig.update_yaxes(title_text="Application", row=3, col=1)
    fig.update_xaxes(title_text="EMR Version", row=4, col=1)
    fig.update_yaxes(title_text="Number of Applications", row=4, col=1)
    fig.update_xaxes(title_text="Days Remaining", row=5, col=1)
    fig.update_yaxes(title_text="Application (sorted by urgency)", row=5, col=1)
    fig.update_xaxes(title_text="Days Remaining", row=6, col=1)
    fig.update_yaxes(title_text="Application (sorted by urgency)", row=6, col=1)
    fig.update_xaxes(tickangle=45, row=4, col=1)
    
    fig.write_html(str(output_file))
    print(f"\n✓ Dashboard created: {output_file}")
    
    print("\n=== EMR Migration Summary ===")
    print(f"Total Unique Applications (filename + EMR version): {len(df_grouped)}")
    print(f"Total Raw Application Entries: {len(df)}")
    print(f"EMR Versions: {sorted(df_grouped['EMR Version'].unique(), key=sort_version_key)}")
    print(f"Spark Versions: {sorted(df_grouped['Spark Version'].unique())}")
    print(f"Total Step Count: {df_grouped['Step Count'].sum()}")
    print(f"High Usage Apps (>2 steps): {len(df_grouped[df_grouped['Step Count'] > 2])}")
    
    return str(output_file)



# ============================================================================
# MAIN EXECUTION
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description='EMR Migration Tool - Assess clusters and generate migration dashboard',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Use default AWS credentials
  python emr_migration_tool.py --region us-east-1
  
  # Use specific AWS profile
  python emr_migration_tool.py --profile my-aws-profile --region us-west-2
  
  # Custom output paths
  python emr_migration_tool.py --region us-east-1 --data-output my_data.json --dashboard-output my_dashboard.html
  
  # Use existing data file (skip assessment)
  python emr_migration_tool.py --data emr_data.json
        """
    )
    
    parser.add_argument('--profile', type=str, help='AWS profile name to use')
    parser.add_argument('--region', type=str, default='us-east-1', help='AWS region (default: us-east-1)')
    parser.add_argument('--data', type=str, help='Path to existing data JSON file (skip assessment)')
    parser.add_argument('--data-output', type=str, default='emr_data.json',
                        help='Output JSON data file path (default: emr_data.json)')
    parser.add_argument('--dashboard-output', type=str, default='emr_migration_dashboard.html', 
                        help='Output HTML dashboard file path (default: emr_migration_dashboard.html)')
    
    args = parser.parse_args()
    
    print("=" * 80)
    print("EMR Migration Tool")
    print("=" * 80)
    
    # Load or generate data
    if args.data:
        print(f"\n✓ Loading data from {args.data}")
        try:
            with open(args.data, 'r') as f:
                apps = json.load(f)
            print(f"✓ Loaded {len(apps)} applications")
        except Exception as e:
            print(f"✗ Error loading data file: {e}")
            sys.exit(1)
    else:
        print(f"\n→ Assessing EMR clusters in region: {args.region}")
        if args.profile:
            print(f"→ Using AWS profile: {args.profile}")
        
        try:
            apps = assess_emr_upgrades(region=args.region, profile=args.profile)
            print(f"✓ Found {len(apps)} applications")
            
            # Save data to JSON
            with open(args.data_output, 'w') as f:
                json.dump(apps, f, indent=2)
            print(f"✓ Data saved to {args.data_output}")
        except Exception as e:
            print(f"✗ Error during assessment: {e}")
            sys.exit(1)
    
    # Generate dashboard
    print(f"\n→ Generating dashboard...")
    try:
        dashboard_file = create_dashboard(apps, output_file=args.dashboard_output)
        print(f"\n{'=' * 80}")
        print(f"✓ SUCCESS! Open {dashboard_file} in your browser to view the dashboard.")
        print(f"{'=' * 80}")
    except Exception as e:
        print(f"✗ Error generating dashboard: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == '__main__':
    main()

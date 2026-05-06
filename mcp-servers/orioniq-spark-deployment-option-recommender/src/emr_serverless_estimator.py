#!/usr/bin/env python3
"""
EMR Serverless Cost Estimator - Enhanced Version
Parses Spark event logs and estimates EMR Serverless costs based on actual resource utilization.
Enhanced with advanced analytics for workload characterization and performance analysis.
"""

import json
import sys
import statistics
import yaml
import os
from datetime import datetime
from typing import Dict, List, Tuple, Optional
from collections import defaultdict, Counter
from dataclasses import dataclass

# Load configuration
def load_config(config_path="config.yaml"):
    """Load configuration from YAML file"""
    try:
        with open(config_path, 'r') as file:
            config = yaml.safe_load(file)
        return config
    except FileNotFoundError:
        print(f"Warning: Configuration file {config_path} not found. Using default values.")
        return {}
    except yaml.YAMLError as e:
        print(f"Error parsing YAML configuration: {e}")
        return {}

# Load global configuration
CONFIG = load_config()

# EMR Serverless pricing - use configured values or defaults (US East 1)
SERVERLESS_VCPU_PER_HOUR = CONFIG.get('emr_serverless_pricing', {}).get('vcpu_per_hour', 0.052624)
SERVERLESS_GB_PER_HOUR = CONFIG.get('emr_serverless_pricing', {}).get('gb_per_hour', 0.0057785)

@dataclass
class TaskMetrics:
    """Container for task-level metrics"""
    task_id: int
    stage_id: int
    executor_id: str
    duration_ms: int
    memory_bytes_spilled: int
    disk_bytes_spilled: int
    shuffle_read_bytes: int
    shuffle_write_bytes: int
    input_bytes: int
    output_bytes: int
    failed: bool
    failure_reason: str = ""
    gc_time_ms: int = 0

@dataclass
class StageMetrics:
    """Container for stage-level metrics"""
    stage_id: int
    stage_name: str
    num_tasks: int
    completion_time: int
    failure_reason: str = ""
    
@dataclass
class WorkloadCharacteristics:
    """Container for workload analysis results"""
    workload_type: str  # CPU_INTENSIVE, MEMORY_INTENSIVE, IO_INTENSIVE, BALANCED
    confidence_score: float
    key_indicators: List[str]
    performance_issues: List[str]
    recommendations: List[str]

def parse_event_log(file_path: str) -> Dict:
    """Parse Spark event log and extract comprehensive resource utilization and performance metrics."""
    
    app_start_time = None
    app_end_time = None
    driver_cores = 0
    driver_memory_mb = 0
    executor_configs = {}
    executor_lifetimes = {}
    
    # Advanced analytics data structures
    task_metrics = []
    stage_metrics = {}
    executor_metrics_updates = defaultdict(list)
    failed_tasks = []
    spill_events = []
    
    # Get progress reporting interval from config
    progress_interval = CONFIG.get('event_log_processing', {}).get('progress_interval', 10000)
    
    print(f"Parsing event log: {file_path}")
    
    try:
        with open(file_path, 'r') as f:
            for line_num, line in enumerate(f, 1):
                if line_num % progress_interval == 0:
                    print(f"Processed {line_num} lines...")
                    
                try:
                    event = json.loads(line.strip())
                    event_type = event.get("Event")
                    
                    if event_type == "SparkListenerApplicationStart":
                        app_start_time = event.get("Timestamp")
                        print(f"Application started at: {app_start_time}")
                        
                    elif event_type == "SparkListenerApplicationEnd":
                        app_end_time = event.get("Timestamp")
                        print(f"Application ended at: {app_end_time}")
                        
                    elif event_type == "SparkListenerEnvironmentUpdate":
                        # Extract driver configuration - use configured defaults
                        default_driver_memory = CONFIG.get('spark_defaults', {}).get('driver_memory', '6g')
                        default_driver_cores = CONFIG.get('spark_defaults', {}).get('driver_cores', 5)
                        
                        spark_props = event.get("Spark Properties", {})
                        driver_memory_str = spark_props.get("spark.driver.memory", default_driver_memory)
                        driver_cores = int(spark_props.get("spark.driver.cores", str(default_driver_cores)))
                        
                        # Parse memory string (e.g., "6g" -> 6144 MB)
                        if driver_memory_str.endswith('g'):
                            driver_memory_mb = int(driver_memory_str[:-1]) * 1024
                        elif driver_memory_str.endswith('m'):
                            driver_memory_mb = int(driver_memory_str[:-1])
                        else:
                            driver_memory_mb = int(driver_memory_str)
                            
                        print(f"Driver config: {driver_cores} cores, {driver_memory_mb} MB memory")
                        
                    elif event_type == "SparkListenerExecutorAdded":
                        executor_id = event.get("Executor ID")
                        executor_info = event.get("Executor Info", {})
                        timestamp = event.get("Timestamp")
                        
                        # Use configured defaults for executor configuration
                        default_executor_cores = CONFIG.get('spark_defaults', {}).get('executor_cores', 5)
                        default_executor_memory_mb = CONFIG.get('spark_defaults', {}).get('executor_memory_gb', 18) * 1024
                        
                        total_cores = executor_info.get("Total Cores", default_executor_cores)
                        executor_configs[executor_id] = {
                            "cores": total_cores,
                            "memory_mb": default_executor_memory_mb,
                            "start_time": timestamp
                        }
                        print(f"Executor {executor_id} added: {total_cores} cores, 18432 MB memory")
                        
                    elif event_type == "SparkListenerExecutorRemoved":
                        executor_id = event.get("Executor ID")
                        timestamp = event.get("Timestamp")
                        
                        if executor_id in executor_configs:
                            start_time = executor_configs[executor_id]["start_time"]
                            executor_lifetimes[executor_id] = {
                                "cores": executor_configs[executor_id]["cores"],
                                "memory_mb": executor_configs[executor_id]["memory_mb"],
                                "duration_ms": timestamp - start_time
                            }
                            print(f"Executor {executor_id} removed after {(timestamp - start_time)/1000:.1f} seconds")
                    
                    # NEW: Parse task completion events for advanced analytics
                    elif event_type == "SparkListenerTaskEnd":
                        task_info = event.get("Task Info", {})
                        task_metrics_data = event.get("Task Metrics", {})
                        
                        if task_info and task_metrics_data:
                            task_metric = TaskMetrics(
                                task_id=task_info.get("Task ID", 0),
                                stage_id=task_info.get("Stage ID", 0),
                                executor_id=task_info.get("Executor ID", ""),
                                duration_ms=task_info.get("Finish Time", 0) - task_info.get("Launch Time", 0),
                                memory_bytes_spilled=task_metrics_data.get("Memory Bytes Spilled", 0),
                                disk_bytes_spilled=task_metrics_data.get("Disk Bytes Spilled", 0),
                                shuffle_read_bytes=task_metrics_data.get("Shuffle Read Metrics", {}).get("Remote Bytes Read", 0),
                                shuffle_write_bytes=task_metrics_data.get("Shuffle Write Metrics", {}).get("Bytes Written", 0),
                                input_bytes=task_metrics_data.get("Input Metrics", {}).get("Bytes Read", 0),
                                output_bytes=task_metrics_data.get("Output Metrics", {}).get("Bytes Written", 0),
                                failed=task_info.get("Failed", False),
                                failure_reason=task_info.get("Failure Reason", ""),
                                gc_time_ms=task_metrics_data.get("JVM GC Time", 0)
                            )
                            
                            task_metrics.append(task_metric)
                            
                            # Track failed tasks separately
                            if task_metric.failed:
                                failed_tasks.append(task_metric)
                            
                            # Track spill events
                            if task_metric.memory_bytes_spilled > 0 or task_metric.disk_bytes_spilled > 0:
                                spill_events.append(task_metric)
                    
                    # NEW: Parse stage completion events
                    elif event_type == "SparkListenerStageCompleted":
                        stage_info = event.get("Stage Info", {})
                        stage_id = stage_info.get("Stage ID", 0)
                        
                        stage_metrics[stage_id] = StageMetrics(
                            stage_id=stage_id,
                            stage_name=stage_info.get("Stage Name", ""),
                            num_tasks=stage_info.get("Number of Tasks", 0),
                            completion_time=stage_info.get("Completion Time", 0),
                            failure_reason=stage_info.get("Failure Reason", "")
                        )
                    
                    # NEW: Parse executor metrics updates for JVM monitoring
                    elif event_type == "SparkListenerExecutorMetricsUpdate":
                        executor_id = event.get("Executor ID", "")
                        metrics = event.get("Metrics", {})
                        timestamp = event.get("Timestamp", 0)
                        
                        if executor_id and metrics:
                            executor_metrics_updates[executor_id].append({
                                "timestamp": timestamp,
                                "jvm_heap_memory": metrics.get("JVMHeapMemory", 0),
                                "jvm_off_heap_memory": metrics.get("JVMOffHeapMemory", 0),
                                "on_heap_execution_memory": metrics.get("OnHeapExecutionMemory", 0),
                                "off_heap_execution_memory": metrics.get("OffHeapExecutionMemory", 0),
                                "on_heap_storage_memory": metrics.get("OnHeapStorageMemory", 0),
                                "off_heap_storage_memory": metrics.get("OffHeapStorageMemory", 0)
                            })
                            
                except json.JSONDecodeError:
                    continue
                except Exception as e:
                    print(f"Error processing line {line_num}: {e}")
                    continue
                    
    except FileNotFoundError:
        print(f"Error: File {file_path} not found")
        return {}
    except Exception as e:
        print(f"Error reading file: {e}")
        return {}
    
    # Calculate total application duration
    if app_start_time and app_end_time:
        app_duration_ms = app_end_time - app_start_time
        print(f"Total application duration: {app_duration_ms/1000/60:.1f} minutes")
        
        # For executors still running at app end, use app duration
        for executor_id, config in executor_configs.items():
            if executor_id not in executor_lifetimes:
                executor_lifetimes[executor_id] = {
                    "cores": config["cores"],
                    "memory_mb": config["memory_mb"],
                    "duration_ms": app_end_time - config["start_time"]
                }
                print(f"Executor {executor_id} ran for full duration: {(app_end_time - config['start_time'])/1000/60:.1f} minutes")
        
        # Driver runs for full application duration
        driver_duration_ms = app_duration_ms
    else:
        print("Warning: Could not determine application duration")
        app_duration_ms = 0
        driver_duration_ms = 0
    
    return {
        "app_duration_ms": app_duration_ms,
        "driver_cores": driver_cores,
        "driver_memory_mb": driver_memory_mb,
        "driver_duration_ms": driver_duration_ms,
        "executor_lifetimes": executor_lifetimes,
        # Enhanced analytics data
        "task_metrics": task_metrics,
        "stage_metrics": stage_metrics,
        "executor_metrics_updates": dict(executor_metrics_updates),
        "failed_tasks": failed_tasks,
        "spill_events": spill_events
    }

def analyze_workload_characteristics(resource_data: Dict) -> WorkloadCharacteristics:
    """Analyze workload characteristics to determine if it's CPU, memory, or I/O intensive."""
    
    task_metrics = resource_data.get("task_metrics", [])
    spill_events = resource_data.get("spill_events", [])
    failed_tasks = resource_data.get("failed_tasks", [])
    
    if not task_metrics:
        return WorkloadCharacteristics(
            workload_type="UNKNOWN",
            confidence_score=0.0,
            key_indicators=["No task metrics available"],
            performance_issues=[],
            recommendations=["Enable Spark event logging for detailed analysis"]
        )
    
    # Calculate key metrics
    total_tasks = len(task_metrics)
    total_spill_bytes = sum(t.memory_bytes_spilled + t.disk_bytes_spilled for t in task_metrics)
    total_shuffle_bytes = sum(t.shuffle_read_bytes + t.shuffle_write_bytes for t in task_metrics)
    total_io_bytes = sum(t.input_bytes + t.output_bytes for t in task_metrics)
    total_gc_time = sum(t.gc_time_ms for t in task_metrics)
    total_task_time = sum(t.duration_ms for t in task_metrics)
    
    # Get analysis thresholds from config
    spill_threshold = CONFIG.get('workload_analysis', {}).get('spill_ratio_threshold', 0.1)
    gc_threshold = CONFIG.get('workload_analysis', {}).get('gc_ratio_threshold', 0.1)
    failure_threshold = CONFIG.get('workload_analysis', {}).get('failure_rate_threshold', 0.05)
    skew_threshold = CONFIG.get('workload_analysis', {}).get('skew_factor_threshold', 3)
    shuffle_multiplier = CONFIG.get('workload_analysis', {}).get('shuffle_io_multiplier', 2)
    confidence_threshold = CONFIG.get('workload_analysis', {}).get('confidence_threshold', 0.3)
    
    # Calculate ratios and percentages
    spill_ratio = total_spill_bytes / max(1, total_io_bytes) if total_io_bytes > 0 else 0
    gc_ratio = total_gc_time / max(1, total_task_time) if total_task_time > 0 else 0
    failure_rate = len(failed_tasks) / max(1, total_tasks)
    
    # Task duration analysis for skew detection
    task_durations = [t.duration_ms for t in task_metrics if t.duration_ms > 0]
    if task_durations:
        avg_duration = statistics.mean(task_durations)
        max_duration = max(task_durations)
        duration_variance = statistics.variance(task_durations) if len(task_durations) > 1 else 0
        skew_factor = max_duration / avg_duration if avg_duration > 0 else 1
    else:
        avg_duration = max_duration = duration_variance = skew_factor = 0
    
    # Determine workload type based on metrics
    indicators = []
    performance_issues = []
    recommendations = []
    
    # Memory-intensive indicators
    memory_score = 0
    if spill_ratio > spill_threshold:
        memory_score += 3
        indicators.append(f"High spill ratio: {spill_ratio:.1%}")
        performance_issues.append("Memory pressure causing disk spills")
        recommendations.append("Increase executor memory or reduce partition size")
    
    if gc_ratio > gc_threshold:
        memory_score += 2
        indicators.append(f"High GC overhead: {gc_ratio:.1%}")
        performance_issues.append("Excessive garbage collection")
        recommendations.append("Tune JVM heap settings and memory allocation")
    
    # I/O-intensive indicators
    io_score = 0
    if total_shuffle_bytes > total_io_bytes * shuffle_multiplier:
        io_score += 3
        indicators.append("High shuffle-to-I/O ratio")
        performance_issues.append("Excessive data shuffling")
        recommendations.append("Optimize joins and reduce shuffle operations")
    
    # CPU-intensive indicators (by elimination and task patterns)
    cpu_score = 0
    if spill_ratio < spill_threshold/2 and gc_ratio < gc_threshold/2:  # Low memory pressure
        cpu_score += 2
        indicators.append("Low memory pressure, likely CPU-bound")
    
    # Data skew detection
    if skew_factor > skew_threshold:
        performance_issues.append(f"Data skew detected (max/avg duration: {skew_factor:.1f}x)")
        recommendations.append("Repartition data to reduce skew")
    
    # Task failure analysis
    if failure_rate > failure_threshold:
        performance_issues.append(f"High task failure rate: {failure_rate:.1%}")
        recommendations.append("Investigate task failures and increase resource allocation")
    
    # Determine primary workload type
    scores = {"MEMORY_INTENSIVE": memory_score, "IO_INTENSIVE": io_score, "CPU_INTENSIVE": cpu_score}
    workload_type = max(scores, key=scores.get)
    confidence_score = scores[workload_type] / max(1, sum(scores.values()))
    
    if confidence_score < confidence_threshold:
        workload_type = "BALANCED"
        indicators.append("Mixed workload characteristics")
    
    return WorkloadCharacteristics(
        workload_type=workload_type,
        confidence_score=confidence_score,
        key_indicators=indicators,
        performance_issues=performance_issues,
        recommendations=recommendations
    )

def calculate_serverless_cost(resource_data: Dict) -> Dict:
    """Calculate EMR Serverless cost based on resource utilization."""
    
    # Driver resource consumption
    driver_core_seconds = (resource_data["driver_duration_ms"] / 1000) * resource_data["driver_cores"]
    driver_memory_mb_seconds = (resource_data["driver_duration_ms"] / 1000) * resource_data["driver_memory_mb"]
    
    # Executor resource consumption
    total_executor_core_seconds = 0
    total_executor_memory_mb_seconds = 0
    
    for executor_id, lifetime in resource_data["executor_lifetimes"].items():
        duration_seconds = lifetime["duration_ms"] / 1000
        core_seconds = duration_seconds * lifetime["cores"]
        memory_mb_seconds = duration_seconds * lifetime["memory_mb"]
        
        total_executor_core_seconds += core_seconds
        total_executor_memory_mb_seconds += memory_mb_seconds
        
        print(f"Executor {executor_id}: {duration_seconds:.1f}s × {lifetime['cores']} cores = {core_seconds:.1f} core-seconds")
    
    # Total resource consumption
    total_core_seconds = driver_core_seconds + total_executor_core_seconds
    total_memory_mb_seconds = driver_memory_mb_seconds + total_executor_memory_mb_seconds
    
    # Convert to hours and GB
    total_core_hours = total_core_seconds / 3600
    total_memory_gb_hours = total_memory_mb_seconds / 3600 / 1024
    
    # Calculate costs
    estimated_vcpu_cost = total_core_hours * SERVERLESS_VCPU_PER_HOUR
    estimated_memory_cost = total_memory_gb_hours * SERVERLESS_GB_PER_HOUR
    total_estimated_cost = estimated_vcpu_cost + estimated_memory_cost
    
    return {
        "driver_core_seconds": driver_core_seconds,
        "driver_memory_mb_seconds": driver_memory_mb_seconds,
        "total_executor_core_seconds": total_executor_core_seconds,
        "total_executor_memory_mb_seconds": total_executor_memory_mb_seconds,
        "total_core_seconds": total_core_seconds,
        "total_memory_mb_seconds": total_memory_mb_seconds,
        "total_core_hours": total_core_hours,
        "total_memory_gb_hours": total_memory_gb_hours,
        "estimated_vcpu_cost": estimated_vcpu_cost,
        "estimated_memory_cost": estimated_memory_cost,
        "total_estimated_cost": total_estimated_cost
    }

def main():
    if len(sys.argv) != 2:
        print("Usage: python emr_serverless_estimator.py <event_log_file>")
        sys.exit(1)
    
    event_log_file = sys.argv[1]
    
    print("=" * 60)
    print("EMR Serverless Cost Estimator - Enhanced Version")
    print("=" * 60)
    
    # Parse event log
    resource_data = parse_event_log(event_log_file)
    
    if not resource_data:
        print("Failed to parse event log")
        sys.exit(1)
    
    # Calculate costs
    cost_data = calculate_serverless_cost(resource_data)
    
    # Analyze workload characteristics
    workload_analysis = analyze_workload_characteristics(resource_data)
    
    # Print results in CSV format similar to the official tool
    print("\n" + "=" * 60)
    print("RESULTS")
    print("=" * 60)
    
    app_id = event_log_file.split('/')[-1]
    
    print("\napp_id,driver_core_seconds,driver_memory_mb_seconds,total_executor_core_seconds,total_executor_memory_mb_seconds")
    print(f'"{app_id}",{cost_data["driver_core_seconds"]:.0f},{cost_data["driver_memory_mb_seconds"]:.0f},{cost_data["total_executor_core_seconds"]:.0f},{cost_data["total_executor_memory_mb_seconds"]:.0f}')
    
    print("\ntotal_core_seconds,total_memory_mb_seconds")
    print(f'{cost_data["total_core_seconds"]:.0f},{cost_data["total_memory_mb_seconds"]:.0f}')
    
    print("\ntotal_core_hours,total_memory_gb_hours")
    print(f'{cost_data["total_core_hours"]:.4f},{cost_data["total_memory_gb_hours"]:.4f}')
    
    print("\nserverless_vcpu_per_hour_price,serverless_per_GB_per_hour_price")
    print(f'{SERVERLESS_VCPU_PER_HOUR},{SERVERLESS_GB_PER_HOUR}')
    
    print("\nestimated_serverless_vcpu_cost,estimated_serverless_memory_cost,total_estimated_cost")
    print(f'{cost_data["estimated_vcpu_cost"]:.6f},{cost_data["estimated_memory_cost"]:.6f},{cost_data["total_estimated_cost"]:.6f}')
    
    print("\n" + "=" * 60)
    print("BASIC SUMMARY")
    print("=" * 60)
    print(f"Application Duration: {resource_data['app_duration_ms']/1000/60:.1f} minutes")
    print(f"Driver: {resource_data['driver_cores']} cores, {resource_data['driver_memory_mb']/1024:.1f} GB")
    print(f"Executors: {len(resource_data['executor_lifetimes'])} executors")
    print(f"Total vCPU Hours: {cost_data['total_core_hours']:.2f}")
    print(f"Total Memory GB Hours: {cost_data['total_memory_gb_hours']:.2f}")
    print(f"Estimated EMR Serverless Cost: ${cost_data['total_estimated_cost']:.2f}")
    
    # Enhanced analytics output
    print("\n" + "=" * 60)
    print("WORKLOAD ANALYSIS")
    print("=" * 60)
    print(f"Workload Type: {workload_analysis.workload_type}")
    print(f"Confidence Score: {workload_analysis.confidence_score:.1%}")
    
    if workload_analysis.key_indicators:
        print(f"\nKey Indicators:")
        for indicator in workload_analysis.key_indicators:
            print(f"  • {indicator}")
    
    if workload_analysis.performance_issues:
        print(f"\nPerformance Issues Detected:")
        for issue in workload_analysis.performance_issues:
            print(f"  ⚠️  {issue}")
    
    if workload_analysis.recommendations:
        print(f"\nRecommendations:")
        for rec in workload_analysis.recommendations:
            print(f"  💡 {rec}")
    
    # Additional metrics summary
    task_metrics = resource_data.get("task_metrics", [])
    failed_tasks = resource_data.get("failed_tasks", [])
    spill_events = resource_data.get("spill_events", [])
    
    if task_metrics:
        print("\n" + "=" * 60)
        print("DETAILED METRICS")
        print("=" * 60)
        print(f"Total Tasks: {len(task_metrics)}")
        print(f"Failed Tasks: {len(failed_tasks)} ({len(failed_tasks)/len(task_metrics)*100:.1f}%)")
        print(f"Tasks with Spills: {len(spill_events)} ({len(spill_events)/len(task_metrics)*100:.1f}%)")
        
        # Task duration statistics
        task_durations = [t.duration_ms/1000 for t in task_metrics if t.duration_ms > 0]
        if task_durations:
            print(f"Task Duration - Avg: {statistics.mean(task_durations):.1f}s, Max: {max(task_durations):.1f}s, Min: {min(task_durations):.1f}s")
        
        # Spill statistics
        if spill_events:
            total_spill_mb = sum(t.memory_bytes_spilled + t.disk_bytes_spilled for t in spill_events) / (1024*1024)
            print(f"Total Spill: {total_spill_mb:.1f} MB")
        
        # Shuffle statistics
        total_shuffle_read = sum(t.shuffle_read_bytes for t in task_metrics) / (1024*1024*1024)
        total_shuffle_write = sum(t.shuffle_write_bytes for t in task_metrics) / (1024*1024*1024)
        if total_shuffle_read > 0 or total_shuffle_write > 0:
            print(f"Shuffle Read: {total_shuffle_read:.2f} GB, Shuffle Write: {total_shuffle_write:.2f} GB")

if __name__ == "__main__":
    main()

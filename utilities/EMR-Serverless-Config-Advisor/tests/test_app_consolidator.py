"""Unit tests for emr_s_app_consolidator — parsing and capacity aggregation."""
import importlib.util
import os

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(os.path.dirname(HERE), "emr_s_app_consolidator.py")

spec = importlib.util.spec_from_file_location("consolidator", TOOL)
cons = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cons)


def _rec(name, vcpu, mem_gb, max_exec, min_exec=0, drv_cores=4, drv_mem="27G", disk="200G", arch=None):
    """Build a raw Fine Tuner-style recommendation."""
    return {
        "application_name": name,
        "architecture": arch,
        "worker": {"vcpu": vcpu, "memory_gb": mem_gb, "max_executors": max_exec, "min_executors": min_exec},
        "spark_configs": {
            "spark.executor.cores": str(vcpu),
            "spark.executor.memory": f"{mem_gb}g",
            "spark.driver.cores": str(drv_cores),
            "spark.driver.memory": drv_mem,
            "spark.emr-serverless.executor.disk": disk,
            "spark.dynamicAllocation.maxExecutors": str(max_exec),
            "spark.dynamicAllocation.minExecutors": str(min_exec),
        },
    }


def test_mem_parsing():
    assert cons._parse_mem_gb("54G") == 54
    assert cons._parse_mem_gb("8g") == 8
    assert cons._parse_mem_gb("512m") == 0.5
    assert cons._parse_mem_gb(16) == 16


def test_job_demand_peaks():
    d = cons.job_demand_from_rec(_rec("j1", vcpu=4, mem_gb=8, max_exec=10, drv_cores=4, drv_mem="27G", disk="200G"))
    assert d is not None
    assert d.peak_cpu == 10 * 4 + 4          # execs*cores + driver
    assert d.peak_mem_gb == 10 * 8 + 27
    assert d.peak_disk_gb == 10 * 200 + cons.DEFAULT_DRIVER_DISK_GB


def test_job_config_format_parsing():
    """Consolidator must also accept job-config documents (configuration.spark_conf)."""
    jc = {
        "job_name": "jc1",
        "configuration": {
            "spark_conf": {
                "spark.executor.cores": "8", "spark.executor.memory": "54G",
                "spark.driver.cores": "8", "spark.driver.memory": "54G",
                "spark.dynamicAllocation.maxExecutors": "20",
                "spark.emr-serverless.executor.disk": "200G",
            },
            "compute_platform_properties": {"graviton_enabled": True},
        },
    }
    d = cons.job_demand_from_rec(jc)
    assert d is not None
    assert d.exec_cores == 8 and d.exec_mem_gb == 54
    assert d.max_executors == 20
    assert d.arch == "ARM64"


def test_sequential_vs_concurrent():
    jobs = [
        cons.job_demand_from_rec(_rec("small", 4, 8, 10)),   # peak_cpu = 44
        cons.job_demand_from_rec(_rec("big", 8, 54, 20)),    # peak_cpu = 164
    ]
    seq = cons.consolidate(jobs, "sequential", 0, "sequential")
    con = cons.consolidate(jobs, "peak-concurrent", 0, "peak-concurrent")

    # sequential ceiling = largest single job (164 -> rounded up to mult of 4 = 164)
    assert seq["maximumCapacity"]["cpu"] == "164 vCPU"
    # concurrent ceiling = sum (44 + 164 = 208)
    assert con["maximumCapacity"]["cpu"] == "208 vCPU"
    # concurrent must be >= sequential on every dimension
    assert int(con["maximumCapacity"]["memory"].split()[0]) >= int(seq["maximumCapacity"]["memory"].split()[0])


def test_headroom_applied():
    jobs = [cons.job_demand_from_rec(_rec("j", 4, 8, 10))]  # peak_cpu = 44
    r0 = cons.consolidate(jobs, "sequential", 0, "sequential")
    r50 = cons.consolidate(jobs, "sequential", 50, "sequential")
    assert int(r50["maximumCapacity"]["cpu"].split()[0]) > int(r0["maximumCapacity"]["cpu"].split()[0])


def test_n_largest_concurrency():
    jobs = [
        cons.job_demand_from_rec(_rec("a", 4, 8, 10)),   # 44
        cons.job_demand_from_rec(_rec("b", 8, 54, 20)),  # 164
        cons.job_demand_from_rec(_rec("c", 8, 54, 10)),  # 84
    ]
    r2 = cons.consolidate(jobs, "2", 0, "2")
    # 2 largest = 164 + 84 = 248
    assert r2["maximumCapacity"]["cpu"] == "248 vCPU"


def test_architecture_warning():
    jobs = [
        cons.job_demand_from_rec(_rec("a", 4, 8, 10, arch="ARM64")),
        cons.job_demand_from_rec(_rec("b", 4, 8, 10, arch="X86_64")),
    ]
    r = cons.consolidate(jobs, "sequential", 0, "sequential")
    assert any("architecture" in w.lower() for w in r["consistency_warnings"])

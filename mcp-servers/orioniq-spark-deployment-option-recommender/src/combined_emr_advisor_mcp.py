#!/usr/bin/env python3
"""
Combined EMR Best Practices Advisor MCP Server

This server combines the capabilities of both curated knowledge advisor and 
data processing options advisor to provide comprehensive EMR deployment recommendations.

COMBINED CAPABILITIES:
1. [KNOWLEDGE-BASED] - Curated blog and PDF content analysis with semantic search
2. [DATA-DRIVEN] - Real EMR cluster pattern analysis and resource utilization
3. [HYBRID RECOMMENDATIONS] - Combined expert knowledge + empirical data insights
4. [RESOURCE ANALYSIS] - EMR Serverless estimator integration for precise cost analysis

SIMPLIFIED DESIGN:
- No async/await complexity - uses synchronous operations where possible
- Single recommendation method combining all sources
- Streamlined tool interface
- Direct AWS API integration without complex MCP client dependencies

CRITICAL PRICING INSTRUCTION: 
When comparing EMR-EC2 vs EMR-EKS, ALWAYS KNOW that both support spot instances for fair comparison. The advantage of EMR-EKS should be based on superior spot instance management capabilities (multi-AZ, better fault tolerance, graceful draining), NOT on pricing model differences.

KEY INSIGHT: EMR-EKS has superior spot instance management compared to EMR-EC2
NEW: Spark job analysis provides precise workload characterization and cost estimation

"""

import logging
import statistics
import subprocess
import os
import json
import re
import requests
import yaml
from bs4 import BeautifulSoup
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime, timedelta
from collections import defaultdict, Counter
from dataclasses import dataclass, asdict
from enum import Enum
from mcp.server.fastmcp import FastMCP

# Configure logging first
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load main configuration
def load_config(config_path=None):
    """Load configuration from multiple sources with priority order:
    1. Environment variables (highest priority)
    2. Custom config file path
    3. Default config.yaml file
    4. Built-in defaults (lowest priority)
    """
    
    # Start with built-in defaults
    default_config = {
        'aws': {
            'region': 'us-west-2',
            'emr_cluster_id': ''
        },
        'llm': {
            'model_id': 'anthropic.claude-3-5-sonnet-20241022-v2:0',
            'region': 'us-west-2',
            'temperature': 0.1,
            'max_tokens': 4000
        },
        'app': {
            'log_level': 'INFO',
            'max_retries': 3,
            'initial_retry_delay': 10
        },
        'semantic_search': {
            'model_name': 'all-MiniLM-L6-v2',
            'similarity_threshold': 0.1,
            'top_k_results': 5
        },
        'text_processing': {
            'chunk_size_large': 2000,
            'chunk_size_medium': 1500,
            'chunk_size_min': 100
        },
        'http': {
            'timeout': 10,
            'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        },
        'knowledge_sources': {
            'blogs': [
                {
                    "blog_summary": "Spark on YARN vs Spark to Kubernetes. When to use what",
                    "url": "https://medium.com/@ahmed.missaoui.pro_79577/spark-on-yarn-vs-park-on-kubernetes-4d2e34d42518"
                },
                {
                    "blog_summary": "Useful features on Kubernetes(EMR-EKS) which are not in YARN(EMR-EC2)",
                    "url": "https://aws.amazon.com/blogs/big-data/run-fault-tolerant-and-cost-optimized-spark-clusters-using-amazon-emr-on-eks-and-amazon-ec2-spot-instances"
                }
            ],
            'pdfs': []
        }
    }
    
    # Try to load from file (config_path or default)
    if config_path is None:
        # Check environment variable first
        config_path = os.environ.get('EMR_ADVISOR_CONFIG_PATH', 'config.yaml')
    
    file_config = {}
    try:
        if os.path.exists(config_path):
            with open(config_path, 'r') as file:
                file_config = yaml.safe_load(file) or {}
            logger.info(f"✅ Configuration loaded from {config_path}")
        else:
            logger.info(f"📝 Config file {config_path} not found, using defaults + environment variables")
    except yaml.YAMLError as e:
        logger.error(f"Error parsing YAML configuration: {e}")
        file_config = {}
    
    # Merge configurations (file overrides defaults)
    config = merge_configs(default_config, file_config)
    
    # Apply environment variable overrides (highest priority)
    config = apply_env_overrides(config)
    
    return config

def merge_configs(base_config, override_config):
    """Recursively merge configuration dictionaries"""
    result = base_config.copy()
    
    for key, value in override_config.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = merge_configs(result[key], value)
        else:
            result[key] = value
    
    return result

def apply_env_overrides(config):
    """Apply environment variable overrides to configuration"""
    
    # AWS Configuration
    if os.environ.get('AWS_REGION'):
        config['aws']['region'] = os.environ['AWS_REGION']
    if os.environ.get('EMR_CLUSTER_ID'):
        config['aws']['emr_cluster_id'] = os.environ['EMR_CLUSTER_ID']
    if os.environ.get('EMR_CLUSTER_ARN'):
        config['aws']['emr_cluster_arn'] = os.environ['EMR_CLUSTER_ARN']
    
    # LLM Configuration
    if os.environ.get('LLM_MODEL_ID'):
        config['llm']['model_id'] = os.environ['LLM_MODEL_ID']
    if os.environ.get('LLM_TEMPERATURE'):
        config['llm']['temperature'] = float(os.environ['LLM_TEMPERATURE'])
    if os.environ.get('LLM_MAX_TOKENS'):
        config['llm']['max_tokens'] = int(os.environ['LLM_MAX_TOKENS'])
    
    # App Configuration
    if os.environ.get('LOG_LEVEL'):
        config['app']['log_level'] = os.environ['LOG_LEVEL']
    if os.environ.get('MAX_RETRIES'):
        config['app']['max_retries'] = int(os.environ['MAX_RETRIES'])
    
    # Semantic Search Configuration
    if os.environ.get('SEMANTIC_MODEL_NAME'):
        config['semantic_search']['model_name'] = os.environ['SEMANTIC_MODEL_NAME']
    if os.environ.get('SIMILARITY_THRESHOLD'):
        config['semantic_search']['similarity_threshold'] = float(os.environ['SIMILARITY_THRESHOLD'])
    
    # Knowledge Sources (JSON format for complex structures)
    if os.environ.get('KNOWLEDGE_BLOGS_JSON'):
        try:
            import json
            config['knowledge_sources']['blogs'] = json.loads(os.environ['KNOWLEDGE_BLOGS_JSON'])
        except json.JSONDecodeError:
            logger.warning("Invalid JSON in KNOWLEDGE_BLOGS_JSON environment variable")
    
    if os.environ.get('KNOWLEDGE_PDFS_JSON'):
        try:
            import json
            config['knowledge_sources']['pdfs'] = json.loads(os.environ['KNOWLEDGE_PDFS_JSON'])
        except json.JSONDecodeError:
            logger.warning("Invalid JSON in KNOWLEDGE_PDFS_JSON environment variable")
    
    return config

# Load global configuration
CONFIG = load_config()

# Update logging level based on config
log_level = getattr(logging, CONFIG.get('app', {}).get('log_level', 'INFO').upper())
logging.getLogger().setLevel(log_level)

# Load scoring configuration
def load_scoring_config():
    """Load scoring configuration from YAML file"""
    config_path = os.path.join(os.path.dirname(__file__), 'scoring_config.yaml')
    try:
        with open(config_path, 'r') as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        logger.warning(f"Scoring config not found at {config_path}, using defaults")
        return None

SCORING_CONFIG = load_scoring_config()

# Initialize FastMCP server
mcp = FastMCP("dataprocessing-advisor-mcp-server", instructions="""
Combined EMR Best Practices Advisor: Unified knowledge-based and data-driven EMR deployment recommendations.

MANDATORY WORKFLOW:
1. ALWAYS call generate_analysis_plan() FIRST and wait for user confirmation before proceeding
2. Then: collect_workload_context → analyze_spark_job_config → analyze_cluster_patterns → get_emr_pricing
3. NEVER skip generate_analysis_plan — it collects user priorities that affect scoring weights

DISPLAY RULES:
- ALWAYS display the calculation_breakdown from get_emr_pricing results — users need to see the math
- ALWAYS use EXACT cost numbers from tool output — never recalculate or round
""")


# =============================================================================
# SPARK JOB ANALYSIS INTEGRATION
# =============================================================================

# Import Spark job analysis components
import sys
sys.path.append('./emr-best-practices-advisor')
try:
    from emr_serverless_estimator import (
        TaskMetrics, StageMetrics, WorkloadCharacteristics,
        parse_event_log, analyze_workload_characteristics, calculate_serverless_cost,
        SERVERLESS_VCPU_PER_HOUR, SERVERLESS_GB_PER_HOUR
    )
    SPARK_ANALYZER_AVAILABLE = True
    logger.info("✅ Spark analyzer components imported successfully")
except ImportError as e:
    SPARK_ANALYZER_AVAILABLE = False
    logger.error(f"⚠️ Could not import Spark analyzer: {e}")

@dataclass
class SparkJobAnalysis:
    """Container for complete Spark job analysis results"""
    app_duration_ms: int
    driver_cores: int
    driver_memory_mb: int
    executor_count: int
    total_tasks: int
    failed_tasks: int
    workload_characteristics: WorkloadCharacteristics
    cost_analysis: Dict[str, Any]
    performance_summary: Dict[str, Any]
    deployment_scores: List[Dict[str, Any]]


def score_deployment_options(
    duration_minutes: float = 120,
    workload_type: str = "BALANCED",
    failure_rate: float = 0.0,
    estimated_serverless_cost: Optional[float] = None,
    performance_issues: Optional[List[str]] = None,
    has_kubernetes_experience: bool = False,
    job_frequency: str = "low",
    predictability: float = 0.5,
    idle_time_percentage: float = 50.0,
    spot_suitability: float = 0.5,
) -> List[Dict[str, Any]]:
    """
    Single source of truth for deployment option scoring.
    
    Accepts structured metrics (from event logs, cluster analysis, or parsed descriptions)
    and returns scored recommendations for all 3 deployment options.
    """
    ec2_score, eks_score, serverless_score = 50, 50, 50
    ec2_reasons, eks_reasons, serverless_reasons = [], [], []

    # --- Duration ---
    if duration_minutes < 60:
        serverless_score += 20
        serverless_reasons.append(f"Short duration ({duration_minutes:.0f} min) ideal for serverless")
    elif duration_minutes > 240:
        serverless_score -= 15
        ec2_score += 15
        ec2_reasons.append(f"Long duration ({duration_minutes:.0f} min) suits persistent clusters")
        serverless_reasons.append(f"Long duration ({duration_minutes:.0f} min) may be costly on serverless")

    # --- Workload type ---
    wt_adjustments = {
        "CPU_INTENSIVE":    (0, 0, 10, "CPU-intensive works well with serverless auto-scaling"),
        "MEMORY_INTENSIVE": (10, 0, -10, "Memory-intensive needs careful tuning — EC2 gives more control"),
        "IO_INTENSIVE":     (10, 5, -5, "I/O-intensive benefits from local disks (EC2/EKS)"),
    }
    if workload_type in wt_adjustments:
        ec2_adj, eks_adj, sls_adj, reason = wt_adjustments[workload_type]
        ec2_score += ec2_adj; eks_score += eks_adj; serverless_score += sls_adj
        if sls_adj > 0: serverless_reasons.append(reason)
        elif ec2_adj > 0: ec2_reasons.append(reason)

    # --- Failure rate ---
    if failure_rate > 0.05:
        serverless_score -= 15
        eks_score += 10
        eks_reasons.append(f"High failure rate ({failure_rate:.1%}) — EKS has better fault tolerance")

    # --- Cost ---
    if estimated_serverless_cost is not None and estimated_serverless_cost < 1.0:
        serverless_score += 15
        serverless_reasons.append(f"Low cost (${estimated_serverless_cost:.2f}) perfect for pay-per-use")

    # --- Performance issues ---
    if performance_issues:
        serverless_score -= len(performance_issues) * 5
        eks_score += 5

    # --- Kubernetes experience ---
    if has_kubernetes_experience:
        eks_score += 15
        eks_reasons.append("Team has Kubernetes expertise")
    else:
        eks_score -= 20
        eks_reasons.append("❌ No Kubernetes expertise — steep learning curve")

    # --- Frequency ---
    if job_frequency == "low":
        serverless_score += 15
        serverless_reasons.append("Low frequency — pay-per-use eliminates idle costs")
    elif job_frequency == "high":
        ec2_score += 10
        ec2_reasons.append("High frequency — persistent cluster avoids repeated startup")

    # --- Idle time ---
    if idle_time_percentage > 40:
        serverless_score += 15
        serverless_reasons.append(f"High idle time ({idle_time_percentage:.0f}%) — serverless eliminates waste")

    # --- Spot suitability ---
    if spot_suitability > 0.6:
        eks_score += 10
        ec2_score += 5
        eks_reasons.append(f"High spot suitability ({spot_suitability:.2f}) — EKS has superior spot management")

    # Clamp scores
    scores = [
        {"deployment_option": "EMR_EC2", "score": max(0, min(100, ec2_score)),
         "reasons": ec2_reasons, "implementation_complexity": "low"},
        {"deployment_option": "EMR_EKS", "score": max(0, min(100, eks_score)),
         "reasons": eks_reasons, "implementation_complexity": "high" if not has_kubernetes_experience else "medium"},
        {"deployment_option": "EMR_SERVERLESS", "score": max(0, min(100, serverless_score)),
         "reasons": serverless_reasons, "implementation_complexity": "low"},
    ]
    scores.sort(key=lambda x: x["score"], reverse=True)
    return scores

class SparkJobAnalyzer:
    """Enhanced Spark job analyzer with MCP integration"""
    
    def __init__(self):
        self.available = SPARK_ANALYZER_AVAILABLE
    
    def analyze_spark_job(self, event_log_path: str) -> SparkJobAnalysis:
        """Complete Spark job analysis combining all components"""
        
        if not self.available:
            raise RuntimeError("Spark analyzer components not available")
        
        logger.info(f"🔍 Starting comprehensive Spark job analysis for: {event_log_path}")
        
        # Parse event log using existing function
        resource_data = parse_event_log(event_log_path)
        
        if not resource_data:
            raise ValueError("Failed to parse Spark event log")
        
        # Analyze workload characteristics using existing function
        workload_characteristics = analyze_workload_characteristics(resource_data)
        
        # Calculate costs using existing function
        cost_analysis = calculate_serverless_cost(resource_data)
        
        # Create performance summary
        task_metrics = resource_data.get("task_metrics", [])
        failed_tasks = resource_data.get("failed_tasks", [])
        spill_events = resource_data.get("spill_events", [])
        
        performance_summary = {
            "total_tasks": len(task_metrics),
            "failed_tasks": len(failed_tasks),
            "failure_rate": len(failed_tasks) / max(1, len(task_metrics)),
            "tasks_with_spills": len(spill_events),
            "spill_rate": len(spill_events) / max(1, len(task_metrics)),
            "app_duration_minutes": resource_data["app_duration_ms"] / 1000 / 60,
            "executor_count": len(resource_data["executor_lifetimes"])
        }
        
        # Generate deployment scores for all options
        deployment_scores = score_deployment_options(
            duration_minutes=resource_data["app_duration_ms"] / 1000 / 60,
            workload_type=workload_characteristics.workload_type,
            failure_rate=len(failed_tasks) / max(1, len(task_metrics)),
            estimated_serverless_cost=cost_analysis.get("total_estimated_cost"),
            performance_issues=workload_characteristics.performance_issues,
        )
        
        return SparkJobAnalysis(
            app_duration_ms=resource_data["app_duration_ms"],
            driver_cores=resource_data["driver_cores"],
            driver_memory_mb=resource_data["driver_memory_mb"],
            executor_count=len(resource_data["executor_lifetimes"]),
            total_tasks=len(task_metrics),
            failed_tasks=len(failed_tasks),
            workload_characteristics=workload_characteristics,
            cost_analysis=cost_analysis,
            performance_summary=performance_summary,
            deployment_scores=deployment_scores
        )
    
# Initialize Spark job analyzer
spark_analyzer = SparkJobAnalyzer()


# Global storage
blog_content = {}
pdf_content = {}
analysis_cache = {}

# Optional dependencies with graceful fallbacks
try:
    from sentence_transformers import SentenceTransformer
    import numpy as np
    import faiss
    SEMANTIC_SEARCH_AVAILABLE = True
    
    # Use configured model name
    model_name = CONFIG.get('semantic_search', {}).get('model_name', 'all-MiniLM-L6-v2')
    embedding_model = SentenceTransformer(model_name)
    logger.info(f"✅ Semantic search capabilities available with model: {model_name}")
    logger.info(f"✅ Using FAISS for vector search (version {faiss.__version__})")
except ImportError as e:
    SEMANTIC_SEARCH_AVAILABLE = False
    embedding_model = None
    logger.info(f"⚠️ Semantic search not available: {e}")

try:
    import boto3
    AWS_AVAILABLE = True
    logger.info("✅ AWS SDK available")
except ImportError:
    AWS_AVAILABLE = False
    logger.info("⚠️ AWS SDK not available - cluster analysis will be limited")

# Data Models
class JobType(Enum):
    BATCH = "batch"
    INTERACTIVE = "interactive"
    STREAMING = "streaming"
    MIXED = "mixed"

class ResourceIntensity(Enum):
    CPU_INTENSIVE = "cpu_intensive"
    MEMORY_INTENSIVE = "memory_intensive"
    IO_INTENSIVE = "io_intensive"
    BALANCED = "balanced"

class DeploymentOption(Enum):
    EMR_EC2 = "emr_ec2"
    EMR_EKS = "emr_eks"
    EMR_SERVERLESS = "emr_serverless"

@dataclass
class JobPattern:
    cluster_id: str
    job_type: JobType
    resource_intensity: ResourceIntensity
    peak_hours: List[str]
    job_frequency: str
    predictability: float
    resource_efficiency: float
    cost_efficiency: float
    idle_time_percentage: float
    spot_instance_suitability: float

@dataclass
class RecommendationScore:
    deployment_option: DeploymentOption
    score: float
    confidence: float
    reasons: List[str]
    estimated_cost_savings: Optional[str] = None
    implementation_complexity: str = "medium"

@dataclass
class UnifiedRecommendation:
    cluster_id: str
    workload_description: str
    recommended_deployment: DeploymentOption
    primary_score: RecommendationScore
    alternative_options: List[RecommendationScore]
    knowledge_insights: List[str]
    data_insights: List[str]
    cost_analysis: Dict[str, Any]
    action_items: List[str]
    risk_factors: List[str]
    confidence_level: str

# =============================================================================
# KNOWLEDGE-BASED COMPONENTS (from curated_knowledge_advisor_with_pine.py)
# =============================================================================

def curated_blogs():
    """Curated set of blogs for EMR deployment guidance"""
    # Default blogs if not configured
    default_blogs = [
        {"blog_summary": "Spark on YARN vs Spark to Kubernetes. When to use what", 
         "url": "https://medium.com/@ahmed.missaoui.pro_79577/spark-on-yarn-vs-park-on-kubernetes-4d2e34d42518"},
        {"blog_summary": "Useful features on Kubernetes(EMR-EKS) which are not in YARN(EMR-EC2)", 
         "url": "https://aws.amazon.com/blogs/big-data/run-fault-tolerant-and-cost-optimized-spark-clusters-using-amazon-emr-on-eks-and-amazon-ec2-spot-instances"},
        {"blog_summary": "Best practices for running Spark on EKS", 
         "url": "https://aws.amazon.com/blogs/containers/best-practices-for-running-spark-on-amazon-eks"},
         {"blog_summary": "Wix migrated over 5,000 daily Spark workloads from AWS EMR to EMR on EKS, cutting shared-cluster costs by 60% (and dedicated clusters by 35–50%), speeding up startup times, enabling multi-Spark version support, improving spot instance utilization", 
         "url": "https://www.wix.engineering/post/how-wix-slashed-spark-costs-by-60-and-migrated-5-000-daily-workflows-from-emr-to-emr-on-eks"}
    ]
    
    # Use configured blogs if available, otherwise use defaults
    return CONFIG.get('knowledge_sources', {}).get('blogs', default_blogs)

def curated_deployment_pdfs():
    """Curated deployment option PDFs"""
    # Default PDFs if not configured
    default_pdfs = [
        {
            "path": "curated_pdfs/Amazon BigData Deployment Models.pdf",
            "summary": "Detailed comparison between different EMR deployment options - EMR-EC2, EMR-Serverless and Glue"
        }
    ]
    
    # Use configured PDFs if available, otherwise use defaults
    # Paths are used exactly as specified in the configuration
    return CONFIG.get('knowledge_sources', {}).get('pdfs', default_pdfs)

class KnowledgeProcessor:
    """Processes curated knowledge sources (blogs and PDFs)"""
    
    def __init__(self):
        self.blogs_loaded = False
        self.pdfs_loaded = False
    
    def fetch_blog_content(self, url: str) -> Dict[str, Any]:
        """Fetch content from a blog URL"""
        try:
            # Use configured HTTP settings
            user_agent = CONFIG.get('http', {}).get('user_agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36')
            timeout = CONFIG.get('http', {}).get('timeout', 10)
            
            headers = {
                'User-Agent': user_agent
            }
            response = requests.get(url, headers=headers, timeout=timeout)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.content, 'html.parser')
            
            # Extract title
            title = soup.find('title')
            title_text = title.get_text().strip() if title else "No title found"
            
            # Extract main content
            content_selectors = [
                'article', 'main', '.post-content', '.entry-content', 
                '.content', '.post-body', '[role="main"]', '.blog-post'
            ]
            
            content_text = ""
            for selector in content_selectors:
                content_element = soup.select_one(selector)
                if content_element:
                    content_text = content_element.get_text()
                    break
            
            if not content_text:
                content_text = soup.body.get_text() if soup.body else soup.get_text()
            
            # Clean up text
            content_text = re.sub(r'\s+', ' ', content_text).strip()
            
            return {
                'status': 'success',
                'title': title_text,
                'content': content_text,
                'url': url,
                'content_length': len(content_text)
            }
            
        except Exception as e:
            return {
                'status': 'error',
                'message': f'Failed to fetch blog: {str(e)}',
                'url': url
            }
    
    def convert_pdf_to_text(self, pdf_path: str) -> str:
        """Convert PDF to text using available tools"""
        base_name = os.path.splitext(pdf_path)[0]
        output_path = f"{base_name}.txt"
        
        try:
            if not os.path.exists(pdf_path):
                raise FileNotFoundError(f"PDF file not found: {pdf_path}")
            
            # Check if text file already exists and is newer
            if os.path.exists(output_path):
                pdf_mtime = os.path.getmtime(pdf_path)
                txt_mtime = os.path.getmtime(output_path)
                if txt_mtime > pdf_mtime:
                    with open(output_path, 'r', encoding='utf-8') as f:
                        if f.read().strip():
                            return output_path
            
            # Try pdftotext first
            try:
                subprocess.run(['pdftotext', pdf_path, output_path], check=True)
                if os.path.exists(output_path):
                    with open(output_path, 'r', encoding='utf-8') as f:
                        if f.read().strip():
                            return output_path
            except (subprocess.SubprocessError, FileNotFoundError):
                pass
            
            # Fallback to PyPDF2
            try:
                import PyPDF2
                with open(pdf_path, 'rb') as pdf_file, open(output_path, 'w', encoding='utf-8') as text_file:
                    pdf_reader = PyPDF2.PdfReader(pdf_file)
                    for page_num, page in enumerate(pdf_reader.pages):
                        text = page.extract_text()
                        if text:
                            text_file.write(f"--- PAGE {page_num + 1} ---\n{text}\n\n")
                return output_path
            except ImportError:
                logger.error("PyPDF2 not installed. Install with: uv add pypdf2")
                return None
                
        except Exception as e:
            logger.error(f"Error converting PDF to text: {e}")
            return None
    
    def create_vector_store(self, text_path: str) -> Optional[Dict]:
        """Create vector store for semantic search"""
        if not SEMANTIC_SEARCH_AVAILABLE or not embedding_model:
            return None
            
        try:
            with open(text_path, 'r', encoding='utf-8') as f:
                content = f.read()
            
            if not content.strip():
                return None
            
            # Get text processing configuration
            chunk_size_large = CONFIG.get('text_processing', {}).get('chunk_size_large', 2000)
            chunk_size_medium = CONFIG.get('text_processing', {}).get('chunk_size_medium', 1500)
            chunk_size_min = CONFIG.get('text_processing', {}).get('chunk_size_min', 100)
            
            # Split into chunks
            chunks = []
            if "--- PAGE" in content:
                page_sections = re.split(r'--- PAGE \d+ ---', content)
                for section in page_sections:
                    section = section.strip()
                    if section and len(section) > chunk_size_min:
                        if len(section) > chunk_size_large:
                            # Split large sections
                            sentences = re.split(r'[.!?]\s+', section)
                            current_chunk = ""
                            for sentence in sentences:
                                if len(current_chunk + sentence) > chunk_size_medium:
                                    if current_chunk.strip():
                                        chunks.append(current_chunk.strip())
                                    current_chunk = sentence
                                else:
                                    current_chunk += sentence + ". "
                            if current_chunk.strip():
                                chunks.append(current_chunk.strip())
                        else:
                            chunks.append(section)
            else:
                # Split by paragraphs
                paragraphs = content.split('\n\n')
                current_chunk = ""
                for paragraph in paragraphs:
                    paragraph = paragraph.strip()
                    if not paragraph:
                        continue
                    if len(current_chunk + paragraph) > chunk_size_medium:
                        if current_chunk.strip():
                            chunks.append(current_chunk.strip())
                        current_chunk = paragraph
                    else:
                        current_chunk += "\n\n" + paragraph if current_chunk else paragraph
                if current_chunk.strip():
                    chunks.append(current_chunk.strip())
            
            # Filter short chunks
            chunks = [chunk for chunk in chunks if len(chunk.strip()) > chunk_size_min]
            
            if not chunks:
                return None
            
            # Generate embeddings
            chunk_embeddings = []
            chunk_texts = []
            
            for chunk in chunks:
                try:
                    embedding = embedding_model.encode(chunk)
                    chunk_embeddings.append(embedding.tolist())
                    chunk_texts.append(chunk)
                except Exception as e:
                    logger.error(f"Error generating embedding: {e}")
                    continue
            
            if not chunk_embeddings:
                return None
            
            return {
                'embeddings': chunk_embeddings,
                'texts': chunk_texts,
                'metadata': [{'chunk_id': i} for i in range(len(chunk_texts))]
            }
            
        except Exception as e:
            logger.error(f"Error creating vector store: {e}")
            return None
    
    def semantic_search(self, vector_store: Dict, query: str, top_k: int = 5) -> List[Dict]:
        """Perform semantic search on vector store"""
        if not SEMANTIC_SEARCH_AVAILABLE or not embedding_model or not vector_store:
            return []
        
        try:
            import numpy as np
            
            # Generate query embedding
            query_embedding = embedding_model.encode(query)
            
            # Calculate similarities
            similarities = []
            for i, chunk_embedding in enumerate(vector_store['embeddings']):
                try:
                    similarity = np.dot(query_embedding, chunk_embedding) / (
                        np.linalg.norm(query_embedding) * np.linalg.norm(chunk_embedding)
                    )
                    similarities.append({
                        'text': vector_store['texts'][i],
                        'score': float(similarity),
                        'metadata': vector_store['metadata'][i]
                    })
                except Exception:
                    continue
            
            # Sort and filter using configured thresholds
            similarity_threshold = CONFIG.get('semantic_search', {}).get('similarity_threshold', 0.1)
            similarities.sort(key=lambda x: x['score'], reverse=True)
            return [result for result in similarities if result['score'] > similarity_threshold][:top_k]
            
        except Exception as e:
            logger.error(f"Error in semantic search: {e}")
            return []
    
    def load_all_knowledge_sources(self) -> Dict[str, Any]:
        """Load all curated knowledge sources"""
        results = {'blogs': {}, 'pdfs': {}}
        
        # Load blogs
        for blog_info in curated_blogs():
            url = blog_info["url"]
            result = self.fetch_blog_content(url)
            if result['status'] == 'success':
                # Create temporary text file for vector store
                temp_dir = CONFIG.get('temp_directory', '/tmp')
                temp_path = os.path.join(temp_dir, f"blog_{hash(url)}.txt")
                with open(temp_path, 'w', encoding='utf-8') as f:
                    f.write(result['content'])
                
                # Create vector store for semantic search
                vector_store = self.create_vector_store(temp_path)
                
                blog_content[url] = {
                    'title': result['title'],
                    'content': result['content'],
                    'url': url,
                    'summary': blog_info['blog_summary'],
                    'vector_store': vector_store
                }
                results['blogs'][url] = 'success'
            else:
                results['blogs'][url] = result['message']
        
        # Load PDFs
        for pdf_info in curated_deployment_pdfs():
            path = pdf_info["path"]
            try:
                text_path = self.convert_pdf_to_text(path)
                if text_path and os.path.exists(text_path):
                    with open(text_path, 'r', encoding='utf-8') as f:
                        content = f.read()
                    
                    # Create vector store for semantic search
                    vector_store = self.create_vector_store(text_path)
                    
                    pdf_content[path] = {
                        'title': os.path.basename(path),
                        'content': content,
                        'path': path,
                        'summary': pdf_info['summary'],
                        'text_path': text_path,
                        'vector_store': vector_store
                    }
                    results['pdfs'][path] = 'success'
                else:
                    results['pdfs'][path] = 'conversion_failed'
            except Exception as e:
                results['pdfs'][path] = str(e)
        
        self.blogs_loaded = any(status == 'success' for status in results['blogs'].values())
        self.pdfs_loaded = any(status == 'success' for status in results['pdfs'].values())
        
        return results
    
    def search_knowledge_base(self, query: str) -> Dict[str, Any]:
        """Search across all knowledge sources"""
        results = {'blog_results': [], 'pdf_results': []}
        
        # Get search configuration
        top_k_small = CONFIG.get('semantic_search', {}).get('top_k_results_small', 3)
        context_window = CONFIG.get('text_processing', {}).get('context_window', 200)
        max_matches = CONFIG.get('text_processing', {}).get('max_matches_per_source', 3)
        
        # Search blogs (semantic if available, keyword otherwise)
        for url, blog_data in blog_content.items():
            if 'vector_store' in blog_data and blog_data['vector_store']:
                # Semantic search
                semantic_results = self.semantic_search(blog_data['vector_store'], query, top_k=top_k_small)
                if semantic_results:
                    results['blog_results'].append({
                        'title': blog_data['title'],
                        'url': url,
                        'semantic_matches': [
                            {'text': result['text'], 'score': result['score']} 
                            for result in semantic_results
                        ]
                    })
            else:
                # Keyword search fallback
                if query.lower() in blog_data['content'].lower():
                    # Extract context around matches
                    content = blog_data['content']
                    matches = []
                    start = 0
                    while True:
                        pos = content.lower().find(query.lower(), start)
                        if pos == -1:
                            break
                        context_start = max(0, pos - context_window)
                        context_end = min(len(content), pos + context_window)
                        context = content[context_start:context_end]
                        matches.append(context)
                        start = pos + 1
                        if len(matches) >= max_matches:  # Limit matches per blog
                            break
                    
                    if matches:
                        results['blog_results'].append({
                            'title': blog_data['title'],
                            'url': url,
                            'matches': matches
                        })
        
        # Search PDFs (semantic if available, keyword otherwise)
        for path, pdf_data in pdf_content.items():
            if 'vector_store' in pdf_data and pdf_data['vector_store']:
                # Semantic search
                semantic_results = self.semantic_search(pdf_data['vector_store'], query, top_k=top_k_small)
                if semantic_results:
                    results['pdf_results'].append({
                        'title': pdf_data['title'],
                        'path': path,
                        'semantic_matches': [
                            {'text': result['text'], 'score': result['score']} 
                            for result in semantic_results
                        ]
                    })
            else:
                # Keyword search
                if query.lower() in pdf_data['content'].lower():
                    content = pdf_data['content']
                    matches = []
                    start = 0
                    while True:
                        pos = content.lower().find(query.lower(), start)
                        if pos == -1:
                            break
                        context_start = max(0, pos - context_window)
                        context_end = min(len(content), pos + context_window)
                        context = content[context_start:context_end]
                        matches.append(context)
                        start = pos + 1
                        if len(matches) >= max_matches:
                            break
                    
                    if matches:
                        results['pdf_results'].append({
                            'title': pdf_data['title'],
                            'path': path,
                            'keyword_matches': matches
                        })
        
        return results

# =============================================================================
# DATA-DRIVEN COMPONENTS (from dataprocessing_options_advisor_mcp.py)
# =============================================================================
# EMR Data Collection - Now using instruction-based approach with AWS DP MCP server
# =============================================================================

# EMRDataCollector class removed - using AWS DP MCP server tools instead
# Agents should call:
# - manage_aws_emr_clusters(operation="describe-cluster") 
# - manage_aws_emr_ec2_steps(operation="list-steps")

# EMRDataCollector class removed - using instruction-based approach with AWS DP MCP server

class EMRPatternAnalyzer:
    """Analyzes EMR cluster job patterns and resource utilization"""
    
    def analyze_cluster_patterns(self, cluster_data: Dict[str, Any]) -> JobPattern:
        """Analyze patterns for a single cluster"""
        try:
            cluster_info = cluster_data.get('cluster_info', {})
            steps = cluster_data.get('steps', [])
            instances = cluster_data.get('instances', [])
            
            cluster_id = cluster_info.get('Id', 'unknown')
            
            # Analyze job patterns
            job_type = self._determine_job_type(steps)
            resource_intensity = self._analyze_resource_intensity(instances)
            peak_hours = self._identify_peak_hours(steps)
            job_frequency = self._calculate_job_frequency(steps)
            predictability = self._calculate_predictability(steps)
            resource_efficiency = self._calculate_real_resource_efficiency(cluster_info, steps)
            cost_efficiency = self._calculate_cost_efficiency(steps)
            idle_time_percentage = self._calculate_idle_time(steps, cluster_info)
            spot_suitability = self._calculate_spot_suitability(steps, job_type)
            
            return JobPattern(
                cluster_id=cluster_id,
                job_type=job_type,
                resource_intensity=resource_intensity,
                peak_hours=peak_hours,
                job_frequency=job_frequency,
                predictability=predictability,
                resource_efficiency=resource_efficiency,
                cost_efficiency=cost_efficiency,
                idle_time_percentage=idle_time_percentage,
                spot_instance_suitability=spot_suitability
            )
        except Exception as e:
            logger.error(f"Error analyzing cluster patterns: {e}")
            return self._create_default_pattern(cluster_data.get('cluster_info', {}).get('Id', 'unknown'))
    
    def _determine_job_type(self, steps: List[Dict]) -> JobType:
        """Determine primary job type based on step patterns"""
        if not steps:
            return JobType.MIXED
        
        # Analyze step intervals
        intervals = []
        sorted_steps = sorted(steps, key=lambda x: x.get('Status', {}).get('Timeline', {}).get('CreationDateTime', ''))
        
        for i in range(1, len(sorted_steps)):
            prev_step = sorted_steps[i-1]
            curr_step = sorted_steps[i]
            
            prev_time = prev_step.get('Status', {}).get('Timeline', {}).get('EndDateTime')
            curr_time = curr_step.get('Status', {}).get('Timeline', {}).get('CreationDateTime')
            
            if prev_time and curr_time:
                try:
                    prev_dt = datetime.fromisoformat(prev_time.replace('Z', '+00:00'))
                    curr_dt = datetime.fromisoformat(curr_time.replace('Z', '+00:00'))
                    interval = (curr_dt - prev_dt).total_seconds()
                    intervals.append(interval)
                except:
                    continue
        
        if not intervals:
            return JobType.MIXED
        
        avg_interval = statistics.mean(intervals)
        interval_variance = statistics.variance(intervals) if len(intervals) > 1 else 0
        
        # Get job classification thresholds from config
        batch_threshold = CONFIG.get('job_classification', {}).get('batch_job_threshold', 86400)  # 24 hours
        interactive_threshold = CONFIG.get('job_classification', {}).get('interactive_job_threshold', 3600)  # 1 hour
        variance_threshold = CONFIG.get('job_classification', {}).get('interval_variance_threshold', 0.1)  # 10%
        
        # Classification logic
        if avg_interval > batch_threshold:
            return JobType.BATCH
        elif avg_interval < interactive_threshold and interval_variance > avg_interval:
            return JobType.INTERACTIVE
        elif interval_variance < avg_interval * variance_threshold:
            return JobType.STREAMING
        else:
            return JobType.MIXED
    
    def _analyze_resource_intensity(self, instances: List[Dict]) -> ResourceIntensity:
        """Analyze resource intensity based on instance types"""
        if not instances:
            return ResourceIntensity.BALANCED
        
        instance_types = [instance.get('InstanceType', '') for instance in instances]
        instance_counter = Counter(instance_types)
        
        # Get instance type classifications from config
        cpu_intensive_types = CONFIG.get('instance_types', {}).get('cpu_intensive', ['c5', 'c4', 'c3'])
        memory_intensive_types = CONFIG.get('instance_types', {}).get('memory_intensive', ['r5', 'r4', 'r3', 'x1'])
        io_intensive_types = CONFIG.get('instance_types', {}).get('io_intensive', ['i3', 'i2', 'd2'])
        classification_threshold = CONFIG.get('instance_types', {}).get('classification_threshold', 0.6)
        
        cpu_count = sum(count for instance_type, count in instance_counter.items() 
                       if any(cpu_type in instance_type for cpu_type in cpu_intensive_types))
        memory_count = sum(count for instance_type, count in instance_counter.items() 
                          if any(mem_type in instance_type for mem_type in memory_intensive_types))
        io_count = sum(count for instance_type, count in instance_counter.items() 
                      if any(io_type in instance_type for io_type in io_intensive_types))
        
        total_instances = len(instances)
        
        if cpu_count / total_instances > classification_threshold:
            return ResourceIntensity.CPU_INTENSIVE
        elif memory_count / total_instances > classification_threshold:
            return ResourceIntensity.MEMORY_INTENSIVE
        elif io_count / total_instances > classification_threshold:
            return ResourceIntensity.IO_INTENSIVE
        else:
            return ResourceIntensity.BALANCED
    
    def _identify_peak_hours(self, steps: List[Dict]) -> List[str]:
        """Identify peak usage hours"""
        if not steps:
            return []
        
        hour_counts = defaultdict(int)
        
        for step in steps:
            creation_time = step.get('Status', {}).get('Timeline', {}).get('CreationDateTime')
            if creation_time:
                try:
                    dt = datetime.fromisoformat(creation_time.replace('Z', '+00:00'))
                    hour_counts[dt.hour] += 1
                except:
                    continue
        
        if not hour_counts:
            return []
        
        # Get peak hour multiplier from config
        peak_multiplier = CONFIG.get('performance', {}).get('peak_hour_multiplier', 1.5)
        
        avg_activity = statistics.mean(hour_counts.values())
        peak_hours = [f"{hour:02d}:00-{hour+1:02d}:00" for hour, count in hour_counts.items() 
                     if count > avg_activity * peak_multiplier]
        
        return sorted(peak_hours)[:5]
    
    def _calculate_job_frequency(self, steps: List[Dict]) -> str:
        """Calculate job frequency classification"""
        if not steps:
            return "low"
        
        if len(steps) < 2:
            return "low"
        
        # Get time range
        creation_times = []
        for step in steps:
            creation_time = step.get('Status', {}).get('Timeline', {}).get('CreationDateTime')
            if creation_time:
                try:
                    dt = datetime.fromisoformat(creation_time.replace('Z', '+00:00'))
                    creation_times.append(dt)
                except:
                    continue
        
        if len(creation_times) < 2:
            return "low"
        
        time_range_days = (max(creation_times) - min(creation_times)).days
        if time_range_days == 0:
            time_range_days = 1
        
        # Get frequency thresholds from config
        high_freq_threshold = CONFIG.get('job_classification', {}).get('high_frequency_threshold', 10)
        medium_freq_threshold = CONFIG.get('job_classification', {}).get('medium_frequency_threshold', 3)
        
        jobs_per_day = len(steps) / time_range_days
        
        if jobs_per_day > high_freq_threshold:
            return "high"
        elif jobs_per_day > medium_freq_threshold:
            return "medium"
        else:
            return "low"
    
    def _calculate_predictability(self, steps: List[Dict]) -> float:
        """Calculate predictability score"""
        if len(steps) < 3:
            return 0.5
        
        # Analyze timing patterns
        intervals = []
        sorted_steps = sorted(steps, key=lambda x: x.get('Status', {}).get('Timeline', {}).get('CreationDateTime', ''))
        
        for i in range(1, len(sorted_steps)):
            prev_step = sorted_steps[i-1]
            curr_step = sorted_steps[i]
            
            prev_time = prev_step.get('Status', {}).get('Timeline', {}).get('CreationDateTime')
            curr_time = curr_step.get('Status', {}).get('Timeline', {}).get('CreationDateTime')
            
            if prev_time and curr_time:
                try:
                    prev_dt = datetime.fromisoformat(prev_time.replace('Z', '+00:00'))
                    curr_dt = datetime.fromisoformat(curr_time.replace('Z', '+00:00'))
                    interval = (curr_dt - prev_dt).total_seconds()
                    intervals.append(interval)
                except:
                    continue
        
        if not intervals:
            return 0.5
        
        # Calculate coefficient of variation
        mean_interval = statistics.mean(intervals)
        if mean_interval == 0:
            return 0.5
        
        std_interval = statistics.stdev(intervals) if len(intervals) > 1 else 0
        cv = std_interval / mean_interval
        
        # Get CV divisor from config
        cv_divisor = CONFIG.get('performance', {}).get('predictability_cv_divisor', 2)
        
        # Convert to predictability score
        predictability = max(0, min(1, 1 - (cv / cv_divisor)))
        return predictability
    
    def _calculate_cost_efficiency(self, steps: List[Dict]) -> float:
        """Calculate cost efficiency score"""
        if not steps:
            return 0.5
        
        successful_steps = len([step for step in steps if step.get('Status', {}).get('State') == 'COMPLETED'])
        total_steps = len(steps)
        
        if total_steps == 0:
            return 0.5
        
        # Get success rate multiplier from config
        success_multiplier = CONFIG.get('performance', {}).get('success_rate_multiplier', 1.2)
        
        success_rate = successful_steps / total_steps
        return min(1.0, success_rate * success_multiplier)
    
    def _calculate_idle_time(self, steps: List[Dict], cluster_info: Dict) -> float:
        """Calculate percentage of time cluster was idle"""
        if not steps:
            return 50.0
        
        # calculated from aws dp
        return self._calculate_real_idle_time_percentage(steps, cluster_info)
    
    def _calculate_spot_suitability(self, steps: List[Dict], job_type: JobType) -> float:
        """Calculate suitability for spot instances"""
        if not steps:
            return 0.5
        
        # Calculate average job duration
        durations = []
        for step in steps:
            timeline = step.get('Status', {}).get('Timeline', {})
            start_time = timeline.get('StartDateTime')
            end_time = timeline.get('EndDateTime')
            
            if start_time and end_time:
                try:
                    start_dt = datetime.fromisoformat(start_time.replace('Z', '+00:00'))
                    end_dt = datetime.fromisoformat(end_time.replace('Z', '+00:00'))
                    duration = (end_dt - start_dt).total_seconds()
                    durations.append(duration)
                except:
                    continue
        
        if not durations:
            return 0.5
        
        avg_duration_hours = statistics.mean(durations) / 3600
        
        # Base score by job type
        type_scores = {
            JobType.BATCH: 0.8,
            JobType.STREAMING: 0.3,
            JobType.INTERACTIVE: 0.4,
            JobType.MIXED: 0.6
        }
        
        base_score = type_scores.get(job_type, 0.5)
        
        # Adjust based on duration
        if avg_duration_hours < 1:
            duration_factor = 1.0
        elif avg_duration_hours < 4:
            duration_factor = 0.8
        else:
            duration_factor = 0.6
        
        return min(1.0, base_score * duration_factor)
    
    def _calculate_real_resource_efficiency(self, cluster_info: Dict, steps: List[Dict]) -> float:
        """ Calculate actual resource efficiency from timeline data"""
        timeline = cluster_info.get('Status', {}).get('Timeline', {})
        if not timeline or not steps:
            return 0.1
        
        try:
            creation = timeline.get('CreationDateTime')
            end_time_val = timeline.get('EndDateTime')
            if not creation or not end_time_val:
                return 0.1
            
            if isinstance(creation, str):
                creation = datetime.fromisoformat(creation.replace('Z', '+00:00'))
            if isinstance(end_time_val, str):
                end_time_val = datetime.fromisoformat(end_time_val.replace('Z', '+00:00'))
            
            total_runtime = (end_time_val - creation).total_seconds()
            
            active_time = 0
            for step in steps:
                step_timeline = step.get('Status', {}).get('Timeline', {})
                s_start = step_timeline.get('StartDateTime')
                s_end = step_timeline.get('EndDateTime')
                if s_start and s_end:
                    if isinstance(s_start, str):
                        s_start = datetime.fromisoformat(s_start.replace('Z', '+00:00'))
                    if isinstance(s_end, str):
                        s_end = datetime.fromisoformat(s_end.replace('Z', '+00:00'))
                    active_time += (s_end - s_start).total_seconds()
            
            efficiency = active_time / total_runtime if total_runtime > 0 else 0
            return min(efficiency, 1.0)
            
        except Exception as e:
            logger.error(f"Error calculating resource efficiency: {e}")
            return 0.1
    
    def _calculate_real_idle_time_percentage(self, steps: List[Dict], cluster_info: Dict) -> float:
        """Calculate real idle time percentage from timeline data"""
        timeline = cluster_info.get('Status', {}).get('Timeline', {})
        if not timeline or not steps:
            return 90.0
        
        try:
            creation = timeline.get('CreationDateTime')
            end_time_val = timeline.get('EndDateTime')
            if not creation or not end_time_val:
                return 90.0
            
            # boto3 returns datetime objects, not strings
            if isinstance(creation, str):
                creation = datetime.fromisoformat(creation.replace('Z', '+00:00'))
            if isinstance(end_time_val, str):
                end_time_val = datetime.fromisoformat(end_time_val.replace('Z', '+00:00'))
            
            total_runtime = (end_time_val - creation).total_seconds()
            
            active_time = 0
            for step in steps:
                step_timeline = step.get('Status', {}).get('Timeline', {})
                s_start = step_timeline.get('StartDateTime')
                s_end = step_timeline.get('EndDateTime')
                if s_start and s_end:
                    if isinstance(s_start, str):
                        s_start = datetime.fromisoformat(s_start.replace('Z', '+00:00'))
                    if isinstance(s_end, str):
                        s_end = datetime.fromisoformat(s_end.replace('Z', '+00:00'))
                    active_time += (s_end - s_start).total_seconds()
            
            idle_percentage = ((total_runtime - active_time) / total_runtime) * 100 if total_runtime > 0 else 90.0
            return min(idle_percentage, 99.9)
            
        except Exception as e:
            logger.error(f"Error calculating idle time: {e}")
            return 90.0
    
    def _create_default_pattern(self, cluster_id: str) -> JobPattern:
        """Create default pattern when analysis fails"""
        return JobPattern(
            cluster_id=cluster_id,
            job_type=JobType.MIXED,
            resource_intensity=ResourceIntensity.BALANCED,
            peak_hours=[],
            job_frequency="low",
            predictability=0.5,
            resource_efficiency=0.5,
            cost_efficiency=0.5,
            idle_time_percentage=50.0,
            spot_instance_suitability=0.5
        )

# =============================================================================
# UNIFIED RECOMMENDATION ENGINE
# =============================================================================

class UnifiedRecommendationEngine:
    """Unified recommendation engine combining knowledge-based insights"""
    
    def __init__(self):
        self.knowledge_processor = KnowledgeProcessor()
        # Removed data_collector - using instruction-based approach instead
        self.pattern_analyzer = EMRPatternAnalyzer()
    
    def generate_unified_recommendation(
        self,
        workload_description: str,
        cluster_ids: Optional[List[str]] = None,
        event_log_paths: Optional[List[str]] = None,
        region: str = 'us-east-1',
        profile_name: Optional[str] = "default",
        has_kubernetes_experience: bool = False
    ) -> UnifiedRecommendation:
        """Generate unified recommendation using knowledge-based approach"""
        
        # Load knowledge sources if not already loaded
        if not self.knowledge_processor.blogs_loaded or not self.knowledge_processor.pdfs_loaded:
            logger.info("Loading knowledge sources...")
            self.knowledge_processor.load_all_knowledge_sources()
        
        # 1. Get knowledge-based insights
        knowledge_insights = self._get_knowledge_insights(workload_description)
        
        # 2. Data-driven insights now require AWS DP MCP server
        data_insights = []
        cluster_patterns = []
        if cluster_ids:
            data_insights = [
                "🔍     For accurate cluster analysis, use AWS DP MCP server:",
                "   • Call manage_aws_emr_clusters(operation='describe-cluster')",
                "   • Call manage_aws_emr_ec2_steps(operation='list-steps')",
                "   • Analyze timeline data for real resource efficiency and idle time",
                "   • For EMR-EC2 analyze how much time cluster spends on BOOTSTRAPPING and WAITING stages as this contributes to inefficiences"
            ]
        
        # 3. Analyze workload description
        workload_analysis = self._analyze_workload_description(workload_description)
        
        # 4. Generate unified scores for each deployment option
        deployment_scores = self._calculate_unified_scores(
            workload_analysis,
            workload_analysis["workload_size"], 
            knowledge_insights, 
            data_insights, 
            cluster_patterns,
            has_kubernetes_experience
        )
        
        # 5. Create unified recommendation
        primary_recommendation = max(deployment_scores, key=lambda x: x.score)
        alternative_recommendations = [score for score in deployment_scores if score != primary_recommendation]
        alternative_recommendations.sort(key=lambda x: x.score, reverse=True)
        
        # 6. Generate insights and recommendations
        unified_insights = self._generate_unified_insights(
            workload_analysis, knowledge_insights, data_insights, cluster_patterns
        )
        
        cost_analysis = self._generate_cost_analysis(cluster_patterns, primary_recommendation)
        action_items = self._generate_action_items(primary_recommendation, workload_analysis)
        risk_factors = self._identify_risk_factors(primary_recommendation, has_kubernetes_experience)
        
        # Determine confidence level
        confidence_level = self._determine_confidence_level(
            len(knowledge_insights), len(data_insights), len(cluster_patterns)
        )
        
        return UnifiedRecommendation(
            cluster_id=cluster_ids[0] if cluster_ids else "workload_analysis",
            workload_description=workload_description,
            recommended_deployment=primary_recommendation.deployment_option,
            primary_score=primary_recommendation,
            alternative_options=alternative_recommendations,
            knowledge_insights=knowledge_insights,
            data_insights=data_insights,
            cost_analysis=cost_analysis,
            action_items=action_items,
            risk_factors=risk_factors,
            confidence_level=confidence_level
        )
    
    def _get_knowledge_insights(self, workload_description: str) -> List[str]:
        """Extract insights from knowledge sources"""
        insights = []
        
        # Search knowledge base
        search_results = self.knowledge_processor.search_knowledge_base(workload_description)
        
        # Extract key insights from blog results
        for blog_result in search_results.get('blog_results', []):
            if 'matches' in blog_result:
                insights.append(f"📚 Blog Insight ({blog_result['title']}): Key considerations found")
        
        # Extract key insights from PDF results
        for pdf_result in search_results.get('pdf_results', []):
            if 'semantic_matches' in pdf_result:
                insights.append(f"📄 PDF Insight ({pdf_result['title']}): Deployment comparison available")
            elif 'keyword_matches' in pdf_result:
                insights.append(f"📄 PDF Insight ({pdf_result['title']}): Relevant information found")
        
        # Add general knowledge insights
        insights.extend([
            "🎯 EMR-EKS provides superior spot instance management with multi-AZ deployment",
            "💡 EMR-Serverless eliminates infrastructure management for intermittent workloads",
            "⚖️ EMR-EC2 offers full control but requires more operational overhead"
        ])
        
        return insights
    
    def _extract_data_insights(self, cluster_patterns: List[JobPattern]) -> List[str]:
        """Extract insights from cluster data analysis"""
        insights = []
        
        if not cluster_patterns:
            return insights
        
        # Analyze patterns across clusters
        job_types = [pattern.job_type.value for pattern in cluster_patterns]
        avg_idle_time = sum(pattern.idle_time_percentage for pattern in cluster_patterns) / len(cluster_patterns)
        avg_spot_suitability = sum(pattern.spot_instance_suitability for pattern in cluster_patterns) / len(cluster_patterns)
        avg_predictability = sum(pattern.predictability for pattern in cluster_patterns) / len(cluster_patterns)
        
        # Generate insights
        most_common_job_type = max(set(job_types), key=job_types.count)
        insights.append(f"📊 Data Insight: Most common job type is {most_common_job_type}")
        
        if avg_idle_time > 40:
            insights.append(f"💰 Data Insight: High average idle time ({avg_idle_time:.1f}%) indicates cost optimization opportunity")
        
        if avg_spot_suitability > 0.6:
            insights.append(f"🎯 Data Insight: Workloads are suitable for spot instances (score: {avg_spot_suitability:.2f})")
        
        if avg_predictability > 0.7:
            insights.append(f"📈 Data Insight: Highly predictable workloads (score: {avg_predictability:.2f}) - good for reserved instances")
        
        return insights
    
    def _analyze_workload_description(self, description: str) -> Dict[str, Any]:
        """Analyze workload description to extract characteristics"""
        description_lower = description.lower()
        
        # Determine job type
        if any(word in description_lower for word in ['batch', 'scheduled', 'daily', 'nightly']):
            job_type = 'batch'
        elif any(word in description_lower for word in ['interactive', 'query', 'ad-hoc', 'exploration']):
            job_type = 'interactive'
        elif any(word in description_lower for word in ['streaming', 'real-time', 'continuous']):
            job_type = 'streaming'
        else:
            job_type = 'mixed'
        
        # Determine frequency
        if any(word in description_lower for word in ['frequent', 'continuous', 'high volume']):
            frequency = 'high'
        elif any(word in description_lower for word in ['occasional', 'periodic', 'weekly']):
            frequency = 'medium'
        else:
            frequency = 'low'
        
        # Determine predictability
        if any(word in description_lower for word in ['scheduled', 'regular', 'predictable']):
            predictability = 0.8
        elif any(word in description_lower for word in ['ad-hoc', 'unpredictable', 'variable']):
            predictability = 0.3
        else:
            predictability = 0.5

        # Determine workload size
        if any(word in description_lower for word in [
            'large job', 'heavy workload', 'massive', 'terabytes', 'tb', 'petabytes', 'huge dataset'
        ]):
            workload_size = 'large'
        elif any(word in description_lower for word in [
            'medium', 'moderate', 'hundreds of gb', 'hundreds of gigabytes'
        ]):
            workload_size = 'medium'
        elif any(word in description_lower for word in [
            'small', 'lightweight', 'few gb', 'tens of gb', 'tiny'
        ]):
            workload_size = 'small'
        else:
            workload_size = 'unknown'
        
        # Check for specific mentions
        kubernetes_mentioned = any(word in description_lower for word in ['kubernetes', 'k8s', 'container', 'EKS','eks'])
        spot_mentioned = any(phrase in description_lower for phrase in [
            'spot', 'reduce cost', 'cost reduction', 'cost optimization', 
            'save money', 'cheaper', 'cheap', 'lower cost'
        ])
        
        return {
            'job_type': job_type,
            'frequency': frequency,
            'predictability': predictability,
            'kubernetes_mentioned': kubernetes_mentioned,
            'spot_mentioned': spot_mentioned,
            'workload_size': workload_size
        }
    
    def _calculate_unified_scores(
        self,
        workload_analysis: Dict,
        workload_size: str,
        knowledge_insights: List[str],
        data_insights: List[str],
        cluster_patterns: List[JobPattern],
        has_kubernetes_experience: bool
    ) -> List[RecommendationScore]:
        """Calculate unified scores for all deployment options using config"""
        
        # Get scoring config or use defaults
        config = SCORING_CONFIG.get('scoring', {}) if SCORING_CONFIG else {}
        
        # Base scores from config
        base_scores = config.get('base_scores', {'emr_ec2': 50, 'emr_eks': 50, 'emr_serverless': 50})
        ec2_score = base_scores['emr_ec2']
        eks_score = base_scores['emr_eks'] 
        serverless_score = base_scores['emr_serverless']
        
        # Reasons tracking
        ec2_reasons = []
        eks_reasons = []
        serverless_reasons = []
        
        # Job type scoring from config
        job_type = workload_analysis['job_type']
        job_scoring = config.get('job_type_scoring', {}).get(job_type, {})
        if job_scoring:
            ec2_score += job_scoring.get('emr_ec2', 0)
            eks_score += job_scoring.get('emr_eks', 0)
            serverless_score += job_scoring.get('emr_serverless', 0)
            
            if job_scoring.get('emr_ec2', 0) > 0:
                ec2_reasons.append(f"{job_type.title()} jobs work well with dedicated resources")
            if job_scoring.get('emr_eks', 0) > 0:
                eks_reasons.append(f"{job_type.title()} jobs benefit from Kubernetes scheduling")
            if job_scoring.get('emr_serverless', 0) > 0:
                serverless_reasons.append(f"{job_type.title()} jobs ideal for serverless")

        # Workload size scoring from config
        workload_size_scoring = config.get('workload_size_scoring', {}).get(job_type, {}).get(workload_size, {})
        if workload_size_scoring:
            ec2_score += workload_size_scoring.get('emr_ec2', 0)
            eks_score += workload_size_scoring.get('emr_eks', 0)
            serverless_score += workload_size_scoring.get('emr_serverless', 0)
            
            if workload_size_scoring.get('emr_serverless', 0) > 0:
                if workload_size in ['small', 'medium']:
                    serverless_reasons.append(f"{workload_size.title()} {job_type} jobs ideal for serverless")
                else:
                    serverless_reasons.append(f"{workload_size.title()} {job_type} jobs can use serverless but may be costly")
        
        # Frequency scoring from config
        frequency = workload_analysis['frequency']
        freq_scoring = config.get('frequency_scoring', {}).get(frequency, {})
        if freq_scoring:
            ec2_score += freq_scoring.get('emr_ec2', 0)
            eks_score += freq_scoring.get('emr_eks', 0)
            serverless_score += freq_scoring.get('emr_serverless', 0)
            
            if frequency == 'low' and freq_scoring.get('emr_serverless', 0) > 0:
                serverless_reasons.append("Low frequency workloads perfect for serverless pay-per-use")
        
        # Predictability scoring from config
        predictability = workload_analysis['predictability']
        thresholds = config.get('thresholds', {})
        pred_category = 'high' if predictability > thresholds.get('high_predictability', 0.7) else \
                       'low' if predictability < thresholds.get('low_predictability', 0.3) else 'medium'
        
        pred_scoring = config.get('predictability_scoring', {}).get(pred_category, {})
        if pred_scoring:
            ec2_score += pred_scoring.get('emr_ec2', 0)
            eks_score += pred_scoring.get('emr_eks', 0)
            serverless_score += pred_scoring.get('emr_serverless', 0)
        
        # Feature-based scoring from config
        feature_scoring = config.get('feature_scoring', {})
        
        # Spot instance scoring
        if workload_analysis.get('spot_mentioned', False):
            spot_scores = feature_scoring.get('spot_mentioned', {})
            ec2_score += spot_scores.get('emr_ec2', 0)
            eks_score += spot_scores.get('emr_eks', 0)
            serverless_score += spot_scores.get('emr_serverless', 0)
            
            eks_reasons.append("🎯 SUPERIOR spot instance management with multi-AZ")
            ec2_reasons.append("Good spot instance support but limited by single-AZ deployment")
            serverless_reasons.append("❌ No spot instance support")

        # Kubernetes mentioned scoring
        if workload_analysis.get('kubernetes_mentioned', False):
            k8s_mentioned_scoring = feature_scoring.get('kubernetes_mentioned', {})
            if has_kubernetes_experience:
                eks_score += k8s_mentioned_scoring.get('has_experience', {}).get('emr_eks', 0)
                eks_reasons.append("Kubernetes mentioned and team has expertise")
            else:
                eks_score += k8s_mentioned_scoring.get('no_experience', {}).get('emr_eks', 0)
                eks_reasons.append("⚠️ Kubernetes mentioned but team lacks expertise")
        
        # Kubernetes experience scoring
        k8s_scoring = feature_scoring.get('kubernetes_experience', {})
        if has_kubernetes_experience:
            eks_score += k8s_scoring.get('has_experience', {}).get('emr_eks', 0)
            eks_reasons.append("Team has Kubernetes expertise")
        else:
            eks_score += k8s_scoring.get('no_experience', {}).get('emr_eks', 0)
            eks_reasons.append("❌ CRITICAL: Kubernetes expertise required but not available")
        
        # Data-driven adjustments from config
        if cluster_patterns:
            avg_idle_time = sum(p.idle_time_percentage for p in cluster_patterns) / len(cluster_patterns)
            avg_spot_suitability = sum(p.spot_instance_suitability for p in cluster_patterns) / len(cluster_patterns)
            data_adjustments = config.get('data_driven_adjustments', {})
            
            if avg_idle_time > thresholds.get('high_idle_time', 40):
                serverless_score += data_adjustments.get('high_idle_time', {}).get('emr_serverless', 0)
                serverless_reasons.append(f"High idle time ({avg_idle_time:.1f}%) favors serverless")
            
            if avg_spot_suitability > thresholds.get('high_spot_suitability', 0.6):
                spot_suit_scores = data_adjustments.get('high_spot_suitability', {})
                eks_score += spot_suit_scores.get('emr_eks', 0)
                ec2_score += spot_suit_scores.get('emr_ec2', 0)
                eks_reasons.append(f"High spot suitability ({avg_spot_suitability:.2f}) - EKS provides best spot management")
                ec2_reasons.append(f"Good spot suitability ({avg_spot_suitability:.2f}) but single-AZ limitation")

        # Critical override: Ensure EKS > EC2 for spot strategies
        critical_overrides = config.get('critical_overrides', {})
        if (workload_analysis.get('spot_mentioned', False) and 
            has_kubernetes_experience and 
            eks_score <= ec2_score):
            boost = critical_overrides.get('spot_with_k8s_experience', {}).get('ensure_eks_above_ec2', 10)
            eks_score = ec2_score + boost
            eks_reasons.append("🔧 Score boosted due to superior spot instance capabilities")
        
        # Get confidence levels from config
        confidence_config = config.get('confidence_levels', {})
        ec2_confidence = confidence_config.get('emr_ec2', 0.8)
        eks_confidence = confidence_config.get('emr_eks_with_experience' if has_kubernetes_experience else 'emr_eks_no_experience', 0.7)
        serverless_confidence = confidence_config.get('emr_serverless', 0.8)
        
        # Create recommendation scores
        return [
            RecommendationScore(
                deployment_option=DeploymentOption.EMR_EC2,
                score=min(100, max(0, ec2_score)),
                confidence=ec2_confidence,
                reasons=ec2_reasons,
                implementation_complexity="low"
            ),
            RecommendationScore(
                deployment_option=DeploymentOption.EMR_EKS,
                score=min(100, max(0, eks_score)),
                confidence=eks_confidence,
                reasons=eks_reasons,
                implementation_complexity="high" if not has_kubernetes_experience else "medium"
            ),
            RecommendationScore(
                deployment_option=DeploymentOption.EMR_SERVERLESS,
                score=min(100, max(0, serverless_score)),
                confidence=serverless_confidence,
                reasons=serverless_reasons,
                implementation_complexity="low"
            )
        ]
    
    def _generate_unified_insights(
        self,
        workload_analysis: Dict,
        knowledge_insights: List[str],
        data_insights: List[str],
        cluster_patterns: List[JobPattern]
    ) -> List[str]:
        """Generate unified insights combining all sources"""
        insights = []
        
        # Add workload insights
        insights.append(f"🔍 Workload Analysis: {workload_analysis['job_type']} jobs with {workload_analysis['frequency']} frequency")
        
        # Add top knowledge insights
        insights.extend(knowledge_insights[:3])
        
        # Add top data insights
        insights.extend(data_insights[:3])
        
        # Add synthesis insights
        if cluster_patterns and len(cluster_patterns) > 1:
            insights.append(f"📈 Pattern Analysis: Analyzed {len(cluster_patterns)} clusters for data-driven insights")
        
        return insights
    
    def _generate_cost_analysis(self, cluster_patterns: List[JobPattern], primary_recommendation: RecommendationScore) -> Dict[str, Any]:
        """Generate cost analysis"""
        cost_analysis = {
            'estimated_savings': primary_recommendation.estimated_cost_savings or "Analysis needed",
            'cost_factors': [],
            'optimization_opportunities': []
        }
        
        if cluster_patterns:
            avg_idle_time = sum(p.idle_time_percentage for p in cluster_patterns) / len(cluster_patterns)
            if avg_idle_time > 30:
                cost_analysis['optimization_opportunities'].append(f"Reduce idle time from {avg_idle_time:.1f}%")
        
        if primary_recommendation.deployment_option == DeploymentOption.EMR_SERVERLESS:
            cost_analysis['cost_factors'].append("Pay-per-use pricing eliminates idle costs")
        elif primary_recommendation.deployment_option == DeploymentOption.EMR_EKS:
            cost_analysis['cost_factors'].append("Superior spot instance management can reduce costs by 30-50%")
        else:
            cost_analysis['cost_factors'].append("Reserved instances can provide significant savings for predictable workloads")
        
        return cost_analysis
    
    def _generate_action_items(self, primary_recommendation: RecommendationScore, workload_analysis: Dict) -> List[str]:
        """Generate actionable recommendations"""
        actions = []
        
        deployment = primary_recommendation.deployment_option
        
        if deployment == DeploymentOption.EMR_SERVERLESS:
            actions.extend([
                "Evaluate EMR Serverless for pay-per-use cost model",
                "Test workload compatibility with serverless constraints",
                "Set up monitoring for serverless job execution"
            ])
        elif deployment == DeploymentOption.EMR_EKS:
            actions.extend([
                "Ensure team has Kubernetes expertise or plan training",
                "Design multi-AZ EKS cluster architecture",
                "Implement advanced spot instance strategy with pod disruption budgets"
            ])
        else:  # EMR_EC2
            actions.extend([
                "Right-size EC2 instances based on actual usage",
                "Implement spot instance strategy for cost optimization",
                "Consider reserved instances for predictable workloads"
            ])
        
        # Add general actions
        actions.extend([
            "Set up comprehensive monitoring and alerting",
            "Implement cost tracking and optimization policies"
        ])
        
        return actions
    
    def _identify_risk_factors(self, primary_recommendation: RecommendationScore, has_kubernetes_experience: bool) -> List[str]:
        """Identify potential risks"""
        risks = []
        
        deployment = primary_recommendation.deployment_option
        
        if deployment == DeploymentOption.EMR_EKS:
            if not has_kubernetes_experience:
                risks.extend([
                    "❌ CRITICAL: Kubernetes expertise required for management and troubleshooting",
                    "Consider training team or hiring Kubernetes experts before migration"
                ])
            risks.extend([
                "Additional complexity in networking and security configuration",
                "Learning curve for Kubernetes-specific monitoring and debugging"
            ])
        elif deployment == DeploymentOption.EMR_SERVERLESS:
            risks.extend([
                "Limited customization options compared to EC2/EKS",
                "Potential cold start delays for infrequent jobs",
                "May not support all EMR features and applications"
            ])
        else:  # EMR_EC2
            risks.extend([
                "Single-AZ deployment increases spot interruption risk",
                "Requires more operational overhead for cluster management"
            ])
        
        return risks
    
    def _determine_confidence_level(self, knowledge_count: int, data_count: int, cluster_count: int) -> str:
        """Determine overall confidence level"""
        total_sources = knowledge_count + data_count + cluster_count
        
        if total_sources >= 10 and cluster_count > 0:
            return "high"
        elif total_sources >= 5:
            return "medium"
        else:
            return "low"

# =============================================================================
# MCP TOOLS - SIMPLIFIED INTERFACE
# =============================================================================

# Initialize global components
unified_engine = UnifiedRecommendationEngine()

# Workflow state machine — enforces tool call order
_workflow_state = {
    'plan_generated': False,
    'context_collected': False,
    'spark_analyzed': False,
    'cluster_analyzed': False,
}

# Shared data between tools — eliminates agent marshalling non-determinism
_workflow_data = {
    'spark_applications': [],   # set by analyze_cluster_patterns
    'raw_executors': [],        # set by analyze_cluster_patterns
    'cluster_data': {},         # set by analyze_cluster_patterns
    'spark_data': {},           # set by analyze_spark_job_config
}

def _check_workflow(required_steps: List[str], current_tool: str) -> Optional[Dict[str, Any]]:
    """Check if prerequisite workflow steps have been completed. Returns error dict or None."""
    missing = [s for s in required_steps if not _workflow_state.get(s)]
    if not missing:
        return None
    
    step_to_tool = {
        'plan_generated': 'generate_analysis_plan()',
        'context_collected': 'collect_workload_context(preferences=[...])',
        'spark_analyzed': 'analyze_spark_job_config()',
        'cluster_analyzed': 'analyze_cluster_patterns()',
    }
    missing_tools = [step_to_tool[s] for s in missing]
    return {
        'status': 'error',
        'message': f'Cannot call {current_tool} yet. Required prerequisite(s) not completed: {", ".join(missing_tools)}',
        'workflow_order': [
            '1. generate_analysis_plan() — present plan and get user priorities',
            '2. collect_workload_context(preferences=[...]) — gather context with user priorities',
            '3. analyze_spark_job_config() — extract Spark job configuration',
            '4. analyze_cluster_patterns() — analyze cluster utilization',
            '5. get_emr_pricing() — calculate costs and produce recommendation',
        ],
        'current_state': {k: v for k, v in _workflow_state.items()},
    }

@mcp.tool()
def generate_analysis_plan(
    workload_description: str,
    cluster_ids: Optional[List[str]] = None,
    application_ids: Optional[List[str]] = None,
    event_log_paths: Optional[List[str]] = None,
    region: str = 'us-east-1',
    has_kubernetes_experience: bool = False,
) -> Dict[str, Any]:
    """
    Generate an analysis plan for user review BEFORE running any analysis.

    This should be the FIRST tool called. It presents what will be analyzed
    and asks the user to confirm priorities. Do NOT proceed to other tools
    until the user confirms the plan.

    Args:
        workload_description: What the user wants analyzed
        cluster_ids: EMR cluster IDs provided
        application_ids: Spark application IDs provided
        event_log_paths: Event log file paths provided
        region: AWS region
        has_kubernetes_experience: K8s expertise available
    """
    # Reset all state from previous analysis — critical for multi-analysis sessions
    for k in _workflow_state:
        _workflow_state[k] = False
    _workflow_data.clear()
    _workflow_data.update({
        'spark_applications': [],
        'raw_executors': [],
        'cluster_data': {},
        'spark_data': {},
    })

    steps = []
    steps.append("1️⃣ Collect workload context and knowledge base insights")

    if application_ids or event_log_paths:
        source = "Spark History Server" if application_ids else "event log files"
        steps.append(f"2️⃣ Analyze Spark job config from {source}")
    else:
        steps.append("2️⃣ Analyze Spark job config (using defaults — no app IDs or event logs provided)")

    if cluster_ids:
        steps.append(f"3️⃣ Analyze cluster patterns for {', '.join(cluster_ids)} via live AWS APIs")
    else:
        steps.append("3️⃣ Analyze cluster patterns (using estimates — no cluster IDs provided)")

    steps.append("4️⃣ Calculate real-time pricing and produce evidence-based recommendation")

    plan = {
        'status': 'plan_ready',
        'message': 'Please review the analysis plan below and confirm your priorities before I proceed.',
        'analysis_plan': {
            'data_to_analyze': {
                'cluster_ids': cluster_ids or [],
                'application_ids': application_ids or [],
                'event_log_paths': event_log_paths or [],
                'region': region,
                'kubernetes_experience': has_kubernetes_experience,
            },
            'steps': steps,
        },
        'user_action_required': {
            'question': 'What are your top priorities? Pick 1-2:',
            'options': [
                '💰 Lowest cost — minimize spend above all else',
                '🔧 Simplest operations — minimize management overhead',
                '⚡ Best performance — minimize job runtime/latency',
                '🛡️ Highest reliability — multi-AZ, fault tolerance',
                '📈 Best scalability — handle growth and traffic spikes',
            ],
            'also_ask': 'Any constraints? (e.g., no Kubernetes, must use spot, private subnet only)',
        },
        'note': 'After you confirm, I will run the 4 analysis steps and produce an evidence-based recommendation with your priorities factored into the scoring weights.',
    }

    _workflow_state['plan_generated'] = True
    return plan

@mcp.tool()
def load_knowledge_sources() -> Dict[str, Any]:
    """
    Load all curated knowledge sources (blogs and PDFs) for EMR deployment guidance.
    
    This tool loads expert knowledge from:
    - Curated blog posts about EMR deployment options
    - PDF documents with detailed comparisons
    - Creates semantic search indexes for advanced querying
    
    Returns:
        Status of knowledge source loading with details
    """
    try:
        logger.info("📚 Loading curated knowledge sources...")
        
        results = unified_engine.knowledge_processor.load_all_knowledge_sources()
        
        return {
            'status': 'success',
            'blogs_loaded': unified_engine.knowledge_processor.blogs_loaded,
            'pdfs_loaded': unified_engine.knowledge_processor.pdfs_loaded,
            'semantic_search_available': SEMANTIC_SEARCH_AVAILABLE,
            'loading_results': results,
            'knowledge_sources': {
                'blogs': [blog['blog_summary'] for blog in curated_blogs()],
                'pdfs': [pdf['summary'] for pdf in curated_deployment_pdfs()]
            }
        }
        
    except Exception as e:
        logger.error(f"Error loading knowledge sources: {e}")
        return {
            'status': 'error',
            'message': f'Failed to load knowledge sources: {str(e)}',
            'error_type': type(e).__name__
        }

@mcp.tool()
def search_knowledge_base(query: str) -> Dict[str, Any]:
    """
    Search across all loaded knowledge sources for specific information.
    
    This tool searches through:
    - Blog content using keyword matching
    - PDF content using semantic search (if available) or keyword matching
    - Returns relevant excerpts and context
    
    Args:
        query: Search query for finding relevant information
    
    Returns:
        Search results from blogs and PDFs with relevant excerpts
    """
    try:
        logger.info(f"🔍 Searching knowledge base for: {query}")
        
        # Ensure knowledge sources are loaded
        if not unified_engine.knowledge_processor.blogs_loaded or not unified_engine.knowledge_processor.pdfs_loaded:
            unified_engine.knowledge_processor.load_all_knowledge_sources()
        
        search_results = unified_engine.knowledge_processor.search_knowledge_base(query)
        
        return {
            'status': 'success',
            'query': query,
            'blog_results': search_results.get('blog_results', []),
            'pdf_results': search_results.get('pdf_results', []),
            'total_sources_searched': len(blog_content) + len(pdf_content),
            'search_method': 'semantic_and_keyword' if SEMANTIC_SEARCH_AVAILABLE else 'keyword_only'
        }
        
    except Exception as e:
        logger.error(f"Error searching knowledge base: {e}")
        return {
            'status': 'error',
            'message': f'Failed to search knowledge base: {str(e)}',
            'query': query,
            'error_type': type(e).__name__
        }

@mcp.tool()
def analyze_cluster_patterns(
    cluster_ids: Optional[List[str]] = None,
    region: Optional[str] = None,
    user_cluster_runtime_hours: Optional[float] = None,
    user_job_execution_minutes: Optional[float] = None
) -> Dict[str, Any]:
    """
    Analyze EMR cluster patterns using live AWS data.

    Fetches cluster metadata, instances, and steps directly via boto3,
    then runs EMRPatternAnalyzer for workload characterization.

    Args:
        cluster_ids: List of EMR cluster IDs to analyze
        region: AWS region (default from config)
        user_cluster_runtime_hours: Manual override for cluster runtime
        user_job_execution_minutes: Manual override for job execution time

    Returns:
        Cluster analysis with timing, utilization, and deployment pattern insights
    """
    # Enforce workflow order
    workflow_error = _check_workflow(['plan_generated', 'context_collected'], 'analyze_cluster_patterns')
    if workflow_error:
        return workflow_error

    _workflow_state['cluster_analyzed'] = True

    if not cluster_ids:
        return {
            'status': 'success',
            'clusters_analyzed': 0,
            'timing_analysis': {
                'cluster_runtime_hours': user_cluster_runtime_hours or 8.0,
                'total_job_execution_minutes': user_job_execution_minutes or 60,
                'data_source': 'user_provided' if user_cluster_runtime_hours else 'default_estimate',
            },
            'cluster_analysis': {},
            'analysis_details': {'clusters': []}
        }

    if not AWS_AVAILABLE:
        return {'status': 'error', 'message': 'boto3 not available — cannot fetch cluster data'}

    region = region or CONFIG.get('aws', {}).get('region', 'us-west-2')
    emr_client = boto3.client('emr', region_name=region)

    all_cluster_results = []

    for cluster_id in cluster_ids:
        try:
            logger.info(f"Fetching data for cluster {cluster_id} in {region}")

            # Direct boto3 calls — no MCP relay needed
            cluster_resp = emr_client.describe_cluster(ClusterId=cluster_id)
            cluster_info = cluster_resp.get('Cluster', {})

            instances_resp = emr_client.list_instances(ClusterId=cluster_id)
            instances = instances_resp.get('Instances', [])

            # Check autoscaling
            autoscaling_enabled = False
            try:
                scaling_resp = emr_client.get_managed_scaling_policy(ClusterId=cluster_id)
                autoscaling_enabled = scaling_resp.get('ManagedScalingPolicy', {}).get('ComputeLimits') is not None
            except Exception:
                pass

            # Feed into EMRPatternAnalyzer
            cluster_data = {'cluster_info': cluster_info, 'steps': [], 'instances': instances}
            pattern = unified_engine.pattern_analyzer.analyze_cluster_patterns(cluster_data)

            # Extract timing from cluster timeline
            timeline = cluster_info.get('Status', {}).get('Timeline', {})
            creation_time = timeline.get('CreationDateTime')
            ready_time = timeline.get('ReadyDateTime')
            end_time = timeline.get('EndDateTime')

            cluster_runtime_hours = 0
            bootstrapping_minutes = 0
            effective_end_time = end_time or datetime.now(creation_time.tzinfo) if creation_time else None
            if creation_time and effective_end_time:
                cluster_runtime_hours = (effective_end_time - creation_time).total_seconds() / 3600
            if creation_time and ready_time:
                bootstrapping_minutes = (ready_time - creation_time).total_seconds() / 60

            # Spot instance analysis
            spot_count = sum(1 for i in instances if i.get('Market') == 'SPOT')
            spot_pct = spot_count / len(instances) if instances else 0

            # Instance type (most common)
            type_counts = Counter(i.get('InstanceType', '') for i in instances)
            primary_instance_type = type_counts.most_common(1)[0][0] if type_counts else 'unknown'

            # Per-instance runtime for accurate per-second billing
            instance_runtime_details = []
            now = datetime.now(creation_time.tzinfo) if creation_time else None
            for inst in instances:
                inst_tl = inst.get('Status', {}).get('Timeline', {})
                inst_start = inst_tl.get('CreationDateTime')
                inst_end = inst_tl.get('EndDateTime') or end_time or now
                inst_hrs = (inst_end - inst_start).total_seconds() / 3600 if inst_start and inst_end else cluster_runtime_hours
                instance_runtime_details.append({
                    'instance_type': inst.get('InstanceType', primary_instance_type),
                    'market': inst.get('Market', 'ON_DEMAND'),
                    'runtime_hours': round(inst_hrs, 4),
                })

            # Multi-AZ check
            azs = set(i.get('Ec2InstanceAttributes', {}).get('AvailabilityZone', '') for i in instances if i.get('Ec2InstanceAttributes'))
            if not azs:
                azs = set(i.get('AvailabilityZone', '') for i in instances)

            normalized_hours = cluster_info.get('NormalizedInstanceHours', 0)

            # Fetch Spark data directly from EMR Persistent App UI (SHS)
            # Single source for: app discovery + executor metrics + job runtimes
            discovered_app_ids = []
            spark_applications_data = []
            all_raw_executors = []
            total_job_seconds = 0
            try:
                from urllib.parse import urlparse as _urlparse
                cluster_arn = cluster_info.get('ClusterArn', '')
                try:
                    ui_resp = emr_client.create_persistent_app_ui(TargetResourceArn=cluster_arn, Tags=[])
                    ui_id = ui_resp['PersistentAppUIId']
                    logger.info(f"Persistent App UI created/found: {ui_id}")
                except Exception as e:
                    ui_id = f"p-{cluster_id[2:]}"
                    logger.info(f"Using convention UI ID: {ui_id} (create error: {str(e)[:100]})")

                # Retry presigned URL — it may not be ready immediately after UI creation
                import time as _time
                presigned_url = ''
                for _attempt in range(3):
                    url_resp = emr_client.get_persistent_app_ui_presigned_url(PersistentAppUIId=ui_id, PersistentAppUIType='SHS')
                    presigned_url = url_resp.get('PresignedURL', '')
                    if presigned_url:
                        break
                    logger.info(f"Presigned URL not ready (attempt {_attempt+1}/3), waiting 10s...")
                    _time.sleep(10)
                if presigned_url:
                    shs_session = requests.Session()
                    init_resp = shs_session.get(presigned_url, timeout=30, allow_redirects=True)
                    parsed_shs = _urlparse(init_resp.url)
                    shs_base = f"{parsed_shs.scheme}://{parsed_shs.netloc}/shs/api/v1"
                    logger.info(f"SHS base URL: {shs_base}, init status: {init_resp.status_code}")

                    # Discover all applications directly from SHS
                    apps_resp = shs_session.get(f"{shs_base}/applications", timeout=15)
                    if apps_resp.status_code == 200:
                        all_apps = apps_resp.json()
                        for app in all_apps:
                            aid = app.get('id', '')
                            if not aid:
                                continue
                            discovered_app_ids.append({
                                'application_id': aid,
                                'step_name': app.get('name', ''),
                            })

                        # Fetch executor data for each app
                        for app_info in discovered_app_ids:
                            aid = app_info['application_id']
                            try:
                                app_detail = shs_session.get(f"{shs_base}/applications/{aid}", timeout=10)
                                if app_detail.status_code != 200:
                                    continue
                                app_data = app_detail.json()

                                exec_url = f"{shs_base}/applications/{aid}/allexecutors"
                                exec_resp_raw = shs_session.get(exec_url, timeout=10)
                                if exec_resp_raw.status_code != 200:
                                    exec_url = f"{shs_base}/applications/{aid}/1/allexecutors"
                                    exec_resp_raw = shs_session.get(exec_url, timeout=10)
                                if exec_resp_raw.status_code != 200:
                                    continue
                                exec_resp = exec_resp_raw.json()
                                all_raw_executors.extend(exec_resp)
                                non_driver = [e for e in exec_resp if e.get('id') != 'driver']

                                cores = non_driver[0].get('totalCores', 4) if non_driver else 4
                                mem_bytes = non_driver[0].get('maxMemory', 4 * 1073741824) if non_driver else 4 * 1073741824
                                mem_gb = round(mem_bytes / 1073741824, 1)

                                # Job runtime from SHS app duration (in attempts)
                                attempts = app_data.get('attempts', [])
                                duration_ms = attempts[0].get('duration', 0) if attempts else 0
                                app_duration_min = round(duration_ms / 60000, 2)
                                total_job_seconds += duration_ms / 1000

                                spark_applications_data.append({
                                    'application_id': aid,
                                    'job_name': app_data.get('name', app_info.get('step_name', '')),
                                    'job_runtime_minutes': app_duration_min,
                                    'spark_executors': len(non_driver),
                                    'spark_executor_cores': cores,
                                    'spark_executor_memory_gb': mem_gb,
                                    'executor_count_method': 'direct_shs_api',
                                    'workload_characteristics': 'batch_processing',
                                })
                            except Exception as e:
                                logger.warning(f"Failed to fetch Spark data for {aid}: {e}")
                    else:
                        logger.warning(f"SHS /applications endpoint failed: {apps_resp.status_code}")
            except Exception as e:
                logger.warning(f"Failed to access Spark History Server via EMR API: {type(e).__name__}: {e}")

            cluster_result = {
                'cluster_id': cluster_id,
                'timing_analysis': {
                    'cluster_runtime_hours': round(cluster_runtime_hours, 2),
                    'bootstrapping_minutes': round(bootstrapping_minutes, 2),
                    'total_job_execution_minutes': round(total_job_seconds / 60, 2),
                    'normalized_instance_hours': normalized_hours,
                },
                'cluster_analysis': {
                    'spot_instance_percentage': round(spot_pct, 2),
                    'autoscaling_enabled': autoscaling_enabled,
                    'instance_type': primary_instance_type,
                    'multi_az_deployment': len(azs) > 1,
                    'instance_count': len(instances),
                    'step_count': len(discovered_app_ids),
                    'instance_runtime_details': instance_runtime_details,
                },
                'pattern_analysis': {
                    'job_type': pattern.job_type.value,
                    'resource_intensity': pattern.resource_intensity.value,
                    'resource_efficiency': round(
                        (total_job_seconds / (cluster_runtime_hours * 3600)) if (total_job_seconds > 0 and cluster_runtime_hours > 0)
                        else pattern.resource_efficiency, 3),
                    'idle_time_percentage': round(
                        ((1 - total_job_seconds / (cluster_runtime_hours * 3600)) * 100) if (total_job_seconds > 0 and cluster_runtime_hours > 0)
                        else pattern.idle_time_percentage, 1),
                    'spot_instance_suitability': round(pattern.spot_instance_suitability, 2),
                    'predictability': round(pattern.predictability, 2),
                    'job_frequency': pattern.job_frequency,
                    'peak_hours': pattern.peak_hours,
                },
                'cluster_details': {
                    'name': cluster_info.get('Name', ''),
                    'state': cluster_info.get('Status', {}).get('State', ''),
                    'release_label': cluster_info.get('ReleaseLabel', ''),
                },
                'discovered_application_ids': discovered_app_ids,
                'spark_history_server_status': f"Connected — found {len(discovered_app_ids)} apps" if discovered_app_ids else "No apps found (check spark.eventLog.enabled and spark.eventLog.dir)",
                'spark_applications': spark_applications_data,
                'raw_executors': all_raw_executors,
            }
            all_cluster_results.append(cluster_result)

        except Exception as e:
            logger.error(f"Failed to analyze cluster {cluster_id}: {e}")
            all_cluster_results.append({
                'cluster_id': cluster_id,
                'status': 'error',
                'message': str(e)
            })

    # Aggregate if multiple clusters
    primary = all_cluster_results[0] if all_cluster_results else {}

    # Store in shared workflow data — analyze_spark_job_config reads from here
    _workflow_data['spark_applications'] = primary.get('spark_applications', [])
    _workflow_data['raw_executors'] = primary.get('raw_executors', [])
    _workflow_data['cluster_data'] = {
        'timing_analysis': primary.get('timing_analysis', {}),
        'cluster_analysis': primary.get('cluster_analysis', {}),
        'pattern_analysis': primary.get('pattern_analysis', {}),
    }

    has_spark_data = bool(primary.get('spark_applications'))
    discovered_ids = primary.get('discovered_application_ids', [])

    return {
        'status': 'success',
        'NEXT_STEP': 'Call analyze_spark_job_config() with NO arguments.',
        'spark_metrics_available': has_spark_data,
        'applications_found': len(discovered_ids),
        'discovered_application_ids': discovered_ids,
        'clusters_analyzed': len(all_cluster_results),
        'timing_analysis': primary.get('timing_analysis', {}),
        'cluster_analysis': primary.get('cluster_analysis', {}),
        'pattern_analysis': primary.get('pattern_analysis', {}),
        'analysis_timestamp': datetime.utcnow().isoformat(),
    }


@mcp.tool()
def collect_workload_context(
    workload_description: str,
    cluster_ids: Optional[List[str]] = None,
    event_log_paths: Optional[List[str]] = None,
    application_ids: Optional[List[str]] = None,
    region: str = 'us-east-1',
    has_kubernetes_experience: bool = False,
    compliance_requirements: Optional[List[str]] = None,
    preferences: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Step 1: Collect workload context, user constraints, and knowledge base insights.

    This tool does NOT make a deployment recommendation. It collects inputs that
    feed into the final evidence-based recommendation in get_emr_pricing (Step 4).

    Args:
        workload_description: Description of workload requirements
        cluster_ids: EMR cluster IDs for analysis in later steps
        event_log_paths: Spark event log paths for analysis in later steps
        application_ids: Spark application IDs for History Server analysis
        region: AWS region
        has_kubernetes_experience: Whether team has Kubernetes expertise
        compliance_requirements: e.g. ["multi-az", "no-spot", "private-subnet"]
        preferences: e.g. ["minimize-ops", "cost-priority", "performance-priority"]
    """
    try:
        # Enforce workflow order
        workflow_error = _check_workflow(['plan_generated'], 'collect_workload_context')
        if workflow_error:
            return workflow_error

        # Enforce planning phase — priorities must be provided
        if not preferences:
            return {
                'status': 'error',
                'message': 'Missing user priorities. Call generate_analysis_plan() first to collect user priorities, then pass them as the preferences parameter.',
                'required_action': 'Call generate_analysis_plan() and wait for user to select priorities before calling this tool.',
            }

        # Load knowledge sources
        if not unified_engine.knowledge_processor.blogs_loaded or not unified_engine.knowledge_processor.pdfs_loaded:
            unified_engine.knowledge_processor.load_all_knowledge_sources()

        # Parse workload description for structured attributes
        workload_analysis = unified_engine._analyze_workload_description(workload_description)

        # Search knowledge base for relevant insights
        knowledge_results = unified_engine.knowledge_processor.search_knowledge_base(workload_description)

        _workflow_state['context_collected'] = True
        _workflow_data['has_kubernetes_experience'] = has_kubernetes_experience
        _workflow_data['workload_description'] = workload_description
        _workflow_data['preferences'] = preferences or []

        return {
            'status': 'success',
            'step': '1_of_4',
            'workload_context': {
                'description': workload_description,
                'parsed_attributes': workload_analysis,
                'cluster_ids': cluster_ids,
                'event_log_paths': event_log_paths,
                'application_ids': application_ids,
                'region': region,
            },
            'user_constraints': {
                'has_kubernetes_experience': has_kubernetes_experience,
                'compliance_requirements': compliance_requirements or [],
                'preferences': preferences or [],
            },
            'knowledge_insights': {
                'blogs_loaded': unified_engine.knowledge_processor.blogs_loaded,
                'pdfs_loaded': unified_engine.knowledge_processor.pdfs_loaded,
                'relevant_findings': knowledge_results,
            },
            'note': 'No recommendation made yet. Collect Spark metrics (Step 2), cluster metrics (Step 3), and pricing (Step 4) for evidence-based recommendation.',
        }

    except Exception as e:
        logger.error(f"Step 1 error: {e}")
        return {'status': 'error', 'message': str(e)}

def _calculate_peak_concurrent_executors(raw_executors: List[Dict]) -> int:
    """Sweep-line algorithm to find peak concurrent executors from executor timeline."""
    events = []
    # Find the latest removeTime to use as end time for active executors
    all_remove_times = [ex.get('removeTime') for ex in raw_executors if ex.get('removeTime')]
    all_add_times = [ex.get('addTime') for ex in raw_executors if ex.get('addTime')]
    app_end_time = max(all_remove_times) if all_remove_times else (max(all_add_times) if all_add_times else None)

    for ex in raw_executors:
        if ex.get('id') == 'driver':
            continue
        add_time = ex.get('addTime', '')
        remove_time = ex.get('removeTime') or app_end_time
        if add_time:
            events.append((add_time, 1))
        if remove_time:
            events.append((remove_time, -1))

    if not events:
        return len([e for e in raw_executors if e.get('id') != 'driver'])

    events.sort(key=lambda x: x[0])
    current = 0
    peak = 0
    for _, delta in events:
        current += delta
        peak = max(peak, current)
    return peak


@mcp.tool()
def analyze_spark_job_config(
    event_log_paths: Optional[List[str]] = None,
    application_ids: Optional[List[str]] = None,
    history_server_url: Optional[str] = None,
    cluster_arn: Optional[str] = None,
    region: str = 'us-east-1',
    profile_name: Optional[str] = "default",
    extracted_spark_data: Optional[Dict[str, Any]] = None  # NEW
) -> Dict[str, Any]:
    """
    STEP 2 of 4: Extract Spark job configuration from event logs or History Server.
    
    ⚠️ WORKFLOW STATUS:
    1. ✅ collect_workload_context() ← Should be completed
    2. ✅ analyze_spark_job_config() ← You are here  
    3. ❌ analyze_cluster_patterns() ← REQUIRED NEXT
    4. ❌ get_emr_pricing() ← REQUIRED FINAL STEP
    
    This tool extracts Spark job parameters by analyzing:
    - Spark event logs (executor counts, memory, cores, runtime) - LOCAL PARSING
    - Spark History Server applications (via MCP) - RECOMMENDED for large logs
    
    Args:
        event_log_paths: Optional list of Spark event log paths to analyze (local parsing)
        application_ids: Optional list of Spark application IDs to analyze via History Server MCP
        history_server_url: URL of Spark History Server (for self-managed deployments)
        cluster_arn: EMR cluster ARN (for EMR managed deployments with History Server)
        region: AWS region
        profile_name: AWS profile name
    
    Returns:
        Spark configuration analysis for use in next workflow steps
    """
    try:
        # Enforce workflow order
        workflow_error = _check_workflow(['plan_generated', 'context_collected'], 'analyze_spark_job_config')
        if workflow_error:
            return workflow_error

        _workflow_state['spark_analyzed'] = True
        logger.info(f"STEP 2/4: Analyzing Spark job configuration from multiple sources")

        # Priority 1: Use pre-fetched data from _workflow_data (set by analyze_cluster_patterns)
        # This is the deterministic path — no agent marshalling involved
        # analyze_cluster_patterns already set per-app executor count, cores, and memory
        # from the direct SHS API fetch — use as-is, don't overwrite
        if _workflow_data.get('spark_applications'):
            apps = _workflow_data['spark_applications']

            result = {
                'status': 'success',
                'step': '2_of_4',
                'data_source': 'workflow_state_direct_shs',
                'workflow_complete': False,
                'next_required_step': 'get_emr_pricing',
                'analysis_timestamp': datetime.utcnow().isoformat(),
                'applications_analyzed': apps,
            }
            _workflow_data['spark_data'] = result
            return result

        # Priority 2: If agent provides extracted data (fallback)
        if extracted_spark_data:
            # Calculate peak concurrent executors from raw executor list if provided
            apps = extracted_spark_data.get('applications_analyzed', [])
            raw_executors = extracted_spark_data.get('raw_executors', [])
            if not raw_executors and extracted_spark_data.get('applications_analyzed'):
                return {
                    'status': 'error',
                    'message': 'Missing raw_executors. You MUST call list_executors(app_id, include_inactive=true) and pass the ENTIRE response as "raw_executors". Do NOT use get_executor_summary — it lacks the timeline data needed for accurate peak executor calculation.',
                    'required_format': {
                        'extracted_spark_data': {
                            'raw_executors': '[ENTIRE response from list_executors — each entry must have id, addTime, removeTime, totalCores, maxMemory]',
                            'applications_analyzed': [{'application_id': '...', 'job_name': '...', 'job_runtime_minutes': 0, 'workload_characteristics': '...'}],
                        }
                    }
                }

            # Validate that raw_executors have timeline fields (addTime) needed for sweep-line
            non_driver = [e for e in raw_executors if e.get('id') != 'driver']
            if non_driver and not any(e.get('addTime') for e in non_driver):
                return {
                    'status': 'error',
                    'message': 'raw_executors missing addTime field. You passed data from get_executor_summary which lacks executor timelines. Call list_executors(app_id, include_inactive=true) instead and pass the ENTIRE response.',
                }

            peak = _calculate_peak_concurrent_executors(raw_executors)

            # Extract per-executor config from first non-driver executor
            cores_per_executor = 4  # default
            memory_gb_per_executor = 16  # default
            for ex in raw_executors:
                if ex.get('id') != 'driver':
                    cores_per_executor = ex.get('totalCores', 4)
                    mem_bytes = ex.get('maxMemory', 16 * 1073741824)
                    memory_gb_per_executor = round(mem_bytes / 1073741824)
                    break

            for app in apps:
                app['spark_executors'] = peak
                app['spark_executor_cores'] = cores_per_executor
                app['spark_executor_memory_gb'] = memory_gb_per_executor
                app['executor_count_method'] = 'peak_concurrent_sweep_line'

            return {
                'status': 'success',
                'step': '2_of_4',
                'workflow_complete': False,
                'next_required_step': 'analyze_cluster_patterns',
                'analysis_timestamp': datetime.utcnow().isoformat(),
                'applications_analyzed': apps,
                'timing_analysis': extracted_spark_data.get('timing_analysis', {}),
            }

        if not event_log_paths and not application_ids: ## Set defaults if both the info is not provided
            result = {
            'status': 'success',
            'step': '2_of_4',
            'workflow_complete': False,
            'next_required_step': 'analyze_cluster_patterns',
            'analysis_timestamp': datetime.utcnow().isoformat(),
            'sources_analyzed': {
                'event_logs': len(event_log_paths) if event_log_paths else 0,
                'history_server_apps': len(application_ids) if application_ids else 0
            },
            'applications_analyzed': [
                    {
                        'application_id': 'default_workload',
                        'job_name': 'Default Workload',
                        'spark_executors': 10,
                        'spark_executor_cores': 4,
                        'spark_executor_memory_gb': 16,
                        'job_runtime_minutes': 120,
                        'workload_characteristics': 'batch_processing'
                    }
                ],
            'timing_analysis': {
                'total_job_execution_minutes': None,
                'individual_job_times': []
            },
            'analysis_details': {}
        }
            _workflow_data['spark_data'] = result
            return result

        
        
        
        # Extract information from event logs (LOCAL PARSING - may timeout on large logs)
        if event_log_paths:
            job_execution_times = []
            total_execution_seconds = 0
            result = {
                'status': 'success',
                'step': '2_of_4',
                'workflow_complete': False,
                'next_required_step': 'analyze_cluster_patterns',
                'analysis_timestamp': datetime.utcnow().isoformat(),
                'sources_analyzed': {
                    'event_logs': len(event_log_paths)
                },
                'applications_analyzed': [],  # Will be populated from event logs
                'timing_analysis': {
                    'total_job_execution_minutes': None,
                    'individual_job_times': []
                },
                'analysis_details': {}
            }
            
            for log_path in event_log_paths:
                try:
                    if SPARK_ANALYZER_AVAILABLE and os.path.exists(log_path):
                        job_analysis = spark_analyzer.analyze_spark_job(log_path)
                        job_duration = job_analysis.app_duration_ms / 1000
                        job_execution_times.append({
                            'log_path': log_path,
                            'duration_seconds': job_duration,
                            'duration_minutes': job_duration / 60
                        })
                        total_execution_seconds += job_duration

                        app_config = {
                            'application_id': f'event_log_{len(result["applications_analyzed"])}',
                            'job_name': f'Job from {log_path.split("/")[-1]}',
                            'spark_executors': job_analysis.executor_count,
                            'spark_executor_cores': job_analysis.driver_cores,
                            'spark_executor_memory_gb': job_analysis.driver_memory_mb / 1024,
                            'job_runtime_minutes': job_duration / 60,
                            'workload_characteristics': job_analysis.workload_characteristics.workload_type,
                            'deployment_scores': job_analysis.deployment_scores,
                        }
                        result['applications_analyzed'].append(app_config)

                except Exception as e:
                    logger.warning(f"Failed to analyze event log {log_path}: {e}")

            result['timing_analysis']['individual_job_times'] = job_execution_times
            result['timing_analysis']['total_job_execution_minutes'] = total_execution_seconds / 60
            result['analysis_details']['event_logs'] = job_execution_times
            return result
            

        # Enhanced Spark History Server MCP analysis (RECOMMENDED for large applications)
        if application_ids:
            spark_analysis = []
            # Instructions for agents to use Spark History Server MCP with comprehensive analysis
            result = {
                'status': 'pending_analysis',
                'step': '2_of_4',
                'workflow_complete': False,
                'next_required_step': 'complete_spark_history_analysis',
                'analysis_timestamp': datetime.utcnow().isoformat(),
                'sources_analyzed': {
                    'history_server_apps_pending': len(application_ids)
                },
                'applications_analyzed': [],  # Will be populated by agent from History Server MCP
                'timing_analysis': {
                    'total_job_execution_minutes': None,
                    'individual_job_times': []
                },
                'analysis_details': {},
                'agent_action_required': {
                'status': 'MUST_CALL_SPARK_HISTORY_MCP',
                'workflow_blocked': True,
                'applications_pending': application_ids
            },
            }
            result['spark_history_analysis_instructions'] = {
                'required_calls': [
                    'get_application(app_id) — get name, duration, status',
                    'get_executor_summary(app_id) — get aggregated executor metrics',
                    'list_stages(app_id, with_summaries=false) — get stage list WITHOUT summaries (summaries cause timeout on large apps). Use list_slowest_stages(app_id, n=5) instead for performance metrics on key stages.',
                    'list_jobs(app_id) — get job count and statuses',
                ],
                'EXACT_EXTRACTION_RULES': {
                    'raw_executors': 'REQUIRED. From list_executors(app_id): pass the ENTIRE response array as-is. Each entry has id, addTime, removeTime, totalCores, maxMemory, isActive. The tool calculates peak executors, cores per executor, and memory per executor from this — do NOT calculate these yourself.',
                    'job_runtime_minutes': 'From get_application: duration / 60000 (ms to minutes). Round to 2 decimals.',
                    'job_name': 'From get_application: name field.',
                    'workload_characteristics': 'From stage metrics: classify as IO/CPU/memory intensive.',
                },
                'RETURN_FORMAT': {
                    'extracted_spark_data': {
                        'raw_executors': '[ENTIRE list_executors response — do NOT filter or modify]',
                        'applications_analyzed': [{
                            'application_id': 'the app ID',
                            'job_name': 'from get_application.name',
                            'job_runtime_minutes': 'FLOAT — duration / 60000',
                            'workload_characteristics': 'STRING',
                        }]
                    }
                },
                'CRITICAL': 'Do NOT set spark_executors, spark_executor_cores, or spark_executor_memory_gb — the tool extracts these from raw_executors.',
                'THEN_CALL': 'analyze_spark_job_config(extracted_spark_data=<the above>)',
            }

            for app_id in application_ids:
                result['analysis_details'].setdefault('spark_applications', []).append({
                    'application_id': app_id,
                    'status': 'pending_history_server_analysis',
                })

        return result
        
    except Exception as e:
        logger.error(f"STEP 2/4 ERROR: {e}")
        return {
            'status': 'error',
            'step': '2_of_4',
            'message': f'Failed Spark analysis: {str(e)}',
            'applications_analyzed': [
                {
                    'application_id': 'error_fallback',
                    'job_name': 'Fallback Application',
                    'spark_executors': 4,
                    'spark_executor_cores': 4,
                    'spark_executor_memory_gb': 16,
                    'job_runtime_minutes': 120,
                    'workload_characteristics': 'error_fallback'
                }
            ],
            'timing_analysis': {
                'cluster_runtime_hours': 2.0,  # Fallback estimate
                'total_job_execution_minutes': 30,  # Fallback estimate
                'note': 'Using fallback estimates due to analysis error'
            },
            'recommendation': 'Retry with Spark History Server MCP (application_ids) for better reliability'
        }
    
def load_region_mapping():
    """Load region mapping from YAML file"""
    try:
        yaml_path = os.path.join(os.path.dirname(__file__), 'region_mapping.yaml')
        with open(yaml_path, 'r') as f:
            mapping_data = yaml.safe_load(f)
            return mapping_data['region_to_location']
    except Exception as e:
        logger.warning(f"Failed to load region mapping: {e}, using fallback")
        return {
            'us-east-1': 'US East (N. Virginia)',
            'us-west-2': 'US West (Oregon)',
            'eu-west-1': 'Europe (Ireland)',
            'ap-southeast-1': 'Asia Pacific (Singapore)'
        }

def fetch_pricing_rates(region: str, instance_types: Optional[List[str]] = None) -> Dict[str, Any]:
    """Fetch EMR/EC2/EKS pricing rates directly via boto3.
    Fetches rates for each unique instance type for accurate per-instance billing."""
    pricing_client = boto3.client('pricing', region_name='us-east-1')
    
    region_mapping = load_region_mapping()
    location = region_mapping.get(region, 'US East (N. Virginia)')
    
    if not instance_types:
        instance_types = ['m5.xlarge']
    unique_types = sorted(set(instance_types))
    
    rates = {
        'emr_serverless': {'vcpu_hour_rate': 0.052624, 'memory_gb_hour_rate': 0.0057785},
        'emr_eks': {'vcpu_hour_rate': 0.01012, 'memory_gb_hour_rate': 0.00111125},
        'eks_cluster': {'hourly_rate': 0.1},
        'ec2_rates': {},    # instance_type -> hourly_rate
        'emr_rates': {},    # instance_type -> hourly_rate (EMR uplift)
    }

    def _extract_rate(response, usage_type_filter=None):
        for item in response.get('PriceList', []):
            product = json.loads(item) if isinstance(item, str) else item
            if usage_type_filter:
                usage = product.get('product', {}).get('attributes', {}).get('usagetype', '')
                if usage_type_filter not in usage:
                    continue
            terms = product.get('terms', {}).get('OnDemand', {})
            for term in terms.values():
                for dim in term.get('priceDimensions', {}).values():
                    price = float(dim.get('pricePerUnit', {}).get('USD', 0))
                    if price > 0:
                        return price
        return None

    try:
        # EMR Serverless
        resp = pricing_client.get_products(ServiceCode='ElasticMapReduce', MaxResults=5, Filters=[
            {'Type': 'TERM_MATCH', 'Field': 'productFamily', 'Value': 'EMR Serverless'},
            {'Type': 'TERM_MATCH', 'Field': 'location', 'Value': location},
        ])
        rates['emr_serverless']['vcpu_hour_rate'] = _extract_rate(resp, 'vCPUHours') or rates['emr_serverless']['vcpu_hour_rate']
        rates['emr_serverless']['memory_gb_hour_rate'] = _extract_rate(resp, 'MemoryGBHours') or rates['emr_serverless']['memory_gb_hour_rate']

        # EMR on EKS
        resp = pricing_client.get_products(ServiceCode='ElasticMapReduce', MaxResults=5, Filters=[
            {'Type': 'TERM_MATCH', 'Field': 'productFamily', 'Value': 'EMR Containers'},
            {'Type': 'TERM_MATCH', 'Field': 'computeprovider', 'Value': 'EC2'},
            {'Type': 'TERM_MATCH', 'Field': 'location', 'Value': location},
        ])
        rates['emr_eks']['vcpu_hour_rate'] = _extract_rate(resp, 'vCPUHours') or rates['emr_eks']['vcpu_hour_rate']
        rates['emr_eks']['memory_gb_hour_rate'] = _extract_rate(resp, 'GBHours') or rates['emr_eks']['memory_gb_hour_rate']

        # Per-instance-type EC2 and EMR uplift rates
        for itype in unique_types:
            try:
                resp = pricing_client.get_products(ServiceCode='AmazonEC2', MaxResults=1, Filters=[
                    {'Type': 'TERM_MATCH', 'Field': 'instanceType', 'Value': itype},
                    {'Type': 'TERM_MATCH', 'Field': 'tenancy', 'Value': 'Shared'},
                    {'Type': 'TERM_MATCH', 'Field': 'productFamily', 'Value': 'Compute Instance'},
                    {'Type': 'TERM_MATCH', 'Field': 'location', 'Value': location},
                    {'Type': 'TERM_MATCH', 'Field': 'operatingSystem', 'Value': 'Linux'},
                    {'Type': 'TERM_MATCH', 'Field': 'preInstalledSw', 'Value': 'NA'},
                    {'Type': 'TERM_MATCH', 'Field': 'capacitystatus', 'Value': 'Used'},
                ])
                rates['ec2_rates'][itype] = _extract_rate(resp) or 0.192
            except Exception:
                rates['ec2_rates'][itype] = 0.192

            try:
                resp = pricing_client.get_products(ServiceCode='ElasticMapReduce', MaxResults=1, Filters=[
                    {'Type': 'TERM_MATCH', 'Field': 'instanceType', 'Value': itype},
                    {'Type': 'TERM_MATCH', 'Field': 'productFamily', 'Value': 'Elastic Map Reduce Instance'},
                    {'Type': 'TERM_MATCH', 'Field': 'location', 'Value': location},
                    {'Type': 'TERM_MATCH', 'Field': 'softwareType', 'Value': 'EMR'},
                ])
                rates['emr_rates'][itype] = _extract_rate(resp) or 0.048
            except Exception:
                rates['emr_rates'][itype] = 0.048

        # EKS cluster
        resp = pricing_client.get_products(ServiceCode='AmazonEKS', MaxResults=1, Filters=[
            {'Type': 'TERM_MATCH', 'Field': 'productFamily', 'Value': 'Compute'},
            {'Type': 'TERM_MATCH', 'Field': 'operation', 'Value': 'CreateOperation'},
            {'Type': 'TERM_MATCH', 'Field': 'location', 'Value': location},
        ])
        rates['eks_cluster']['hourly_rate'] = _extract_rate(resp) or rates['eks_cluster']['hourly_rate']

    except Exception as e:
        logger.warning(f"Failed to fetch some pricing data, using fallbacks: {e}")

    # Fetch spot prices for each instance type
    rates['spot_rates'] = {}
    try:
        ec2_client = boto3.client('ec2', region_name=region)
        for itype in unique_types:
            try:
                resp = ec2_client.describe_spot_price_history(
                    InstanceTypes=[itype],
                    ProductDescriptions=['Linux/UNIX'],
                    MaxResults=5,
                )
                prices = [float(p['SpotPrice']) for p in resp.get('SpotPriceHistory', [])]
                if prices:
                    rates['spot_rates'][itype] = min(prices)  # cheapest AZ
            except Exception:
                pass
    except Exception as e:
        logger.warning(f"Failed to fetch spot prices: {e}")

    rates['region'] = region
    rates['location'] = location
    return rates

# Priority-aware scoring weights
WEIGHT_PRESETS = {
    'cost':        {'cost': 0.60, 'workload': 0.20, 'ops': 0.15, 'constraints': 0.05},
    'performance': {'cost': 0.15, 'workload': 0.50, 'ops': 0.20, 'constraints': 0.15},
    'operations':  {'cost': 0.20, 'workload': 0.15, 'ops': 0.50, 'constraints': 0.15},
    'reliability': {'cost': 0.15, 'workload': 0.30, 'ops': 0.40, 'constraints': 0.15},
    'scalability': {'cost': 0.15, 'workload': 0.40, 'ops': 0.30, 'constraints': 0.15},
}
DEFAULT_WEIGHTS = {'cost': 0.40, 'workload': 0.30, 'ops': 0.20, 'constraints': 0.10}

def _resolve_weights(preferences: Optional[List[str]] = None) -> Dict[str, float]:
    """Resolve user preferences to scoring weights. The agent normalizes user
    phrasing — we just match canonical keywords. Averages when multiple given."""
    if not preferences:
        return DEFAULT_WEIGHTS.copy()
    matched = []
    for pref in preferences:
        pref_lower = pref.lower()
        for key in WEIGHT_PRESETS:
            if key in pref_lower:
                matched.append(WEIGHT_PRESETS[key])
                break
    if not matched:
        return DEFAULT_WEIGHTS.copy()
    return {k: round(sum(m[k] for m in matched) / len(matched), 2) for k in DEFAULT_WEIGHTS}


def _build_evidence_based_recommendation(
    app_results: List[Dict],
    spark_data: Dict[str, Any],
    cluster_data: Dict[str, Any],
    utilization: float,
    has_kubernetes_experience: bool = False,
    weights: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    """
    Final evidence-based recommendation combining cost, workload fit, operational fit, and constraints.
    This is the ONLY place a deployment recommendation is made — backed by real data.
    Weights are priority-aware — adjusted based on user preferences.
    """
    cluster = cluster_data.get('cluster_analysis', {})
    pattern = cluster_data.get('pattern_analysis', {})

    # --- Cost score (40% weight) — from actual pricing ---
    total_by_option = {'EMR Serverless': 0, 'EMR on EC2': 0, 'EMR on EKS': 0}
    for app in app_results:
        for opt, cost in app.get('cost_analysis', {}).items():
            # cost may be a string like "$5.92 = 90.8 vCPU-hrs..." — extract the number
            if isinstance(cost, str):
                try:
                    cost = float(cost.split('=')[0].replace('$', '').strip())
                except (ValueError, IndexError):
                    cost = 0
            total_by_option[opt] = total_by_option.get(opt, 0) + cost

    cheapest = min(total_by_option.values()) if total_by_option else 1
    cost_scores = {}
    for opt, cost in total_by_option.items():
        cost_scores[opt] = max(0, 100 - ((cost - cheapest) / max(cheapest, 0.01)) * 100)

    # --- Workload fit score (30% weight) — from Spark metrics ---
    workload_scores = {'EMR Serverless': 50, 'EMR on EC2': 50, 'EMR on EKS': 50}
    evidence = {'EMR Serverless': [], 'EMR on EC2': [], 'EMR on EKS': []}

    idle_pct = pattern.get('idle_time_percentage', 50)
    if idle_pct > 40:
        workload_scores['EMR Serverless'] += 30
        evidence['EMR Serverless'].append(f"Cluster {idle_pct:.0f}% idle — pay-per-use eliminates waste")
    else:
        workload_scores['EMR on EC2'] += 15
        evidence['EMR on EC2'].append(f"Cluster {idle_pct:.0f}% idle — persistent cluster is well-utilized")

    resource_intensity = pattern.get('resource_intensity', 'balanced')
    if resource_intensity == 'io_intensive':
        workload_scores['EMR on EC2'] += 20
        workload_scores['EMR Serverless'] -= 15
        evidence['EMR on EC2'].append("IO-intensive workload benefits from local SSDs on EC2")
        evidence['EMR Serverless'].append("IO-intensive workload may degrade on serverless (no local disks)")
    elif resource_intensity == 'cpu_intensive':
        workload_scores['EMR Serverless'] += 15
        evidence['EMR Serverless'].append("CPU-intensive workload scales well on serverless")

    job_type = pattern.get('job_type', 'mixed')
    if job_type == 'streaming':
        workload_scores['EMR on EC2'] += 20
        workload_scores['EMR on EKS'] += 15
        workload_scores['EMR Serverless'] -= 20
        evidence['EMR on EC2'].append("Streaming workloads need persistent clusters")
        evidence['EMR Serverless'].append("Serverless not suited for continuous streaming")

    # --- Operational fit score (20% weight) — from cluster patterns ---
    ops_scores = {'EMR Serverless': 70, 'EMR on EC2': 50, 'EMR on EKS': 40}

    if cluster.get('autoscaling_enabled'):
        ops_scores['EMR on EC2'] += 10
        evidence['EMR on EC2'].append("Autoscaling already configured — operational maturity")

    spot_pct = cluster.get('spot_instance_percentage', 0)
    if spot_pct > 0.3:
        ops_scores['EMR on EKS'] += 20
        evidence['EMR on EKS'].append(f"Using {spot_pct:.0%} spot — EKS has superior spot management with multi-AZ")

    if utilization < 10:
        ops_scores['EMR Serverless'] += 20
        evidence['EMR Serverless'].append(f"Only {utilization:.1f}% utilization — massive waste on persistent clusters")

    # --- Constraint score (10% weight) — from user context ---
    constraint_scores = {'EMR Serverless': 50, 'EMR on EC2': 50, 'EMR on EKS': 50}
    if not has_kubernetes_experience:
        constraint_scores['EMR on EKS'] = 0
        evidence['EMR on EKS'].append("No Kubernetes experience — significant operational risk")

    w = weights or DEFAULT_WEIGHTS

    # --- Weighted final score ---
    final_scores = {}
    for opt in total_by_option:
        final_scores[opt] = round(
            cost_scores.get(opt, 50) * w['cost'] +
            workload_scores.get(opt, 50) * w['workload'] +
            ops_scores.get(opt, 50) * w['ops'] +
            constraint_scores.get(opt, 50) * w['constraints'],
            1
        )

    # Hard constraint: if no K8s experience, cap EKS score below EC2 and Serverless
    if not has_kubernetes_experience and 'EMR on EKS' in final_scores:
        other_scores = [s for o, s in final_scores.items() if o != 'EMR on EKS']
        if other_scores:
            final_scores['EMR on EKS'] = min(final_scores['EMR on EKS'], min(other_scores) - 1)

    ranked = sorted(final_scores.items(), key=lambda x: x[1], reverse=True)
    primary = ranked[0]

    return {
        'primary_recommendation': primary[0],
        'confidence_score': primary[1],
        'ranked_options': [
            {
                'option': opt,
                'score': score,
                'total_cost': round(total_by_option.get(opt, 0), 4),
                'evidence': evidence.get(opt, []),
                'score_breakdown': {
                    'cost_score': round(cost_scores.get(opt, 50), 1),
                    'workload_fit_score': round(workload_scores.get(opt, 50), 1),
                    'operational_fit_score': round(ops_scores.get(opt, 50), 1),
                }
            }
            for opt, score in ranked
        ],
        'scoring_weights': {k: f"{int(v*100)}%" for k, v in w.items()},
        'key_insight': f"Recommended {primary[0]} (score: {primary[1]}) based on actual cluster metrics, Spark workload analysis, and real-time pricing.",
    }


@mcp.tool()
def get_emr_pricing(
    extracted_spark_data: Optional[Dict[str, Any]] = None,
    extracted_cluster_data: Optional[Dict[str, Any]] = None,
    region: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Step 4: Calculate EMR pricing comparison using real-time AWS pricing.

    Fetches pricing directly via boto3 and calculates costs for EMR Serverless,
    EMR on EC2, and EMR on EKS based on Spark config and cluster data from previous steps.

    Args:
        extracted_spark_data: From analyze_spark_job_config. Must contain:
            {
                "applications_analyzed": [
                    {
                        "application_id": "...",
                        "spark_executors": 10,
                        "spark_executor_cores": 4,
                        "spark_executor_memory_gb": 16,
                        "job_runtime_minutes": 52.0,
                        "job_name": "...",
                        "workload_characteristics": "CPU intensive"
                    }
                ]
            }
        extracted_cluster_data: From analyze_cluster_patterns. Must contain:
            {
                "timing_analysis": {
                    "cluster_runtime_hours": 2.0,
                    "total_job_execution_minutes": 52.0,
                    "normalized_instance_hours": 100,
                    "bootstrapping_minutes": 5.0
                },
                "cluster_analysis": {
                    "spot_instance_percentage": 0.3,
                    "autoscaling_enabled": true,
                    "instance_type": "m5.xlarge",
                    "multi_az_deployment": false
                },
                "pattern_analysis": {
                    "idle_time_percentage": 50.0,
                    "resource_intensity": "io_intensive",
                    "job_type": "batch"
                }
            }
        region: AWS region for pricing lookup
    """
    # Enforce workflow order
    workflow_error = _check_workflow(['plan_generated', 'context_collected', 'spark_analyzed', 'cluster_analyzed'], 'get_emr_pricing')
    if workflow_error:
        return workflow_error

    # Read from shared workflow data if agent didn't pass args
    if not extracted_spark_data and _workflow_data.get('spark_data'):
        extracted_spark_data = _workflow_data['spark_data']
    if not extracted_cluster_data and _workflow_data.get('cluster_data'):
        extracted_cluster_data = _workflow_data['cluster_data']

    if not extracted_spark_data or not extracted_cluster_data:
        return {'status': 'error', 'message': 'Missing data from previous steps (need extracted_spark_data and extracted_cluster_data)'}

    if not AWS_AVAILABLE:
        return {'status': 'error', 'message': 'boto3 not available — cannot fetch pricing'}

    region = region or CONFIG.get('aws', {}).get('region', 'us-west-2')

    applications = extracted_spark_data.get('applications_analyzed', [])
    timing = extracted_cluster_data.get('timing_analysis', {})
    cluster = extracted_cluster_data.get('cluster_analysis', {})

    cluster_runtime_hours = timing.get('cluster_runtime_hours', 0)
    instance_count = cluster.get('instance_count', 0)

    # If no real cluster data, estimate from Spark executor metrics
    estimated_cluster = False
    if not instance_count and applications:
        estimated_cluster = True
        # Find peak executors and their resource requirements
        max_executors = max((a.get('spark_executors', 1) for a in applications), default=1)
        max_cores = max((a.get('spark_executor_cores', 4) for a in applications), default=4)
        max_mem_gb = max((a.get('spark_executor_memory_gb', 16) for a in applications), default=16)

        # Pick instance type that fits executors efficiently
        # Try each size and pick the one with best packing (most executors per instance)
        instance_options = [
            ('m5.xlarge',  3, 14),    # 4 vCPU, 16GB → ~3 usable cores, ~14GB
            ('m5.2xlarge', 7, 30),    # 8 vCPU, 32GB → ~7 usable cores, ~30GB
            ('m5.4xlarge', 15, 60),   # 16 vCPU, 64GB → ~15 usable cores, ~60GB
            ('m5.8xlarge', 31, 124),  # 32 vCPU, 128GB → ~31 usable cores, ~124GB
        ]

        best_type, best_epi, best_cores, best_mem = 'm5.4xlarge', 1, 15, 60
        for itype, uc, um in instance_options:
            epc = uc // max_cores
            epm = int(um // max_mem_gb)
            epi = min(epc, epm)
            if epi >= 2:  # Good packing — at least 2 executors per instance
                best_type, best_epi, best_cores, best_mem = itype, epi, uc, um
                break
            elif epi >= 1 and best_epi < epi:
                best_type, best_epi, best_cores, best_mem = itype, epi, uc, um

        estimated_instance_type = best_type
        executors_per_instance = max(1, best_epi)
        core_instances = -(-max_executors // executors_per_instance)  # ceiling division
        instance_count = core_instances + 1  # +1 for master

        # Estimate runtime from longest job + bootstrap overhead
        max_job_minutes = max((a.get('job_runtime_minutes', 60) for a in applications), default=60)
        total_job_minutes = sum(a.get('job_runtime_minutes', 60) for a in applications)
        cluster_runtime_hours = (total_job_minutes + 5) / 60  # +5 min bootstrap

        cluster['instance_type'] = estimated_instance_type
        cluster['instance_count'] = instance_count

    cluster_runtime_hours = cluster_runtime_hours or timing.get('cluster_runtime_hours', 8.0)

    # Fetch real-time pricing for all instance types in the cluster
    instance_details = cluster.get('instance_runtime_details', [])
    all_instance_types = [d['instance_type'] for d in instance_details] if instance_details else [cluster.get('instance_type', 'm5.xlarge')]
    rates = fetch_pricing_rates(region, instance_types=all_instance_types)

    # Rate extraction
    sls_vcpu = rates['emr_serverless']['vcpu_hour_rate']
    sls_mem = rates['emr_serverless']['memory_gb_hour_rate']
    eks_cluster_rate = rates['eks_cluster']['hourly_rate']
    eks_vcpu = rates['emr_eks']['vcpu_hour_rate']
    eks_mem = rates['emr_eks']['memory_gb_hour_rate']

    # === COST BUILDING BLOCKS ===
    # EMR bills per-second (1-min minimum).
    # We calculate raw EC2 cost and EMR EC2 uplift separately so EKS can use EC2 cost without EMR EC2 uplift.

    instance_details = cluster.get('instance_runtime_details', [])

    # 1. Raw EC2 instance cost (on-demand) — used by both EC2 and EKS
    total_ec2_cost = 0
    # 2. EMR EC2 uplift — only for EMR on EC2, NOT for EKS
    total_emr_ec2_uplift = 0
    # 3. Raw EC2 instance cost (spot) — used by both EC2 spot and EKS spot
    total_ec2_spot_cost = 0

    if instance_details:
        for inst in instance_details:
            itype = inst['instance_type']
            hrs = inst['runtime_hours']
            ec2_rate = rates['ec2_rates'].get(itype, 0.192)
            emr_rate = rates['emr_rates'].get(itype, 0.048)
            spot_rate = rates.get('spot_rates', {}).get(itype, ec2_rate)
            total_ec2_cost += ec2_rate * hrs
            total_emr_ec2_uplift += emr_rate * hrs
            total_ec2_spot_cost += spot_rate * hrs
    else:
        primary_type = cluster.get('instance_type', 'm5.xlarge')
        ec2_rate = rates['ec2_rates'].get(primary_type, 0.192)
        emr_rate = rates['emr_rates'].get(primary_type, 0.048)
        spot_rate = rates.get('spot_rates', {}).get(primary_type, ec2_rate)
        total_ec2_cost = instance_count * ec2_rate * cluster_runtime_hours
        total_emr_ec2_uplift = instance_count * emr_rate * cluster_runtime_hours
        # Master on-demand, core nodes on spot
        total_ec2_spot_cost = (1 * ec2_rate + max(0, instance_count - 1) * spot_rate) * cluster_runtime_hours

    # 4. Composite costs for each deployment option
    total_emr_ec2_od = total_ec2_cost + total_emr_ec2_uplift                    # EC2 on-demand
    total_emr_ec2_spot = total_ec2_spot_cost + total_emr_ec2_uplift             # EC2 spot (spot instances + EMR uplift)
    total_emr_eks_od = total_ec2_cost + cluster_runtime_hours * eks_cluster_rate # EKS on-demand (EC2 + EKS cluster fee, NO EMR EC2 uplift)
    total_emr_eks_spot = total_ec2_spot_cost + cluster_runtime_hours * eks_cluster_rate  # EKS spot

    total_job_minutes = sum(a.get('job_runtime_minutes', 120) for a in applications)
    total_vcpu_hours = sum((a.get('job_runtime_minutes', 120) / 60) * a.get('spark_executors', 10) * a.get('spark_executor_cores', 4) for a in applications)
    total_mem_hours = sum((a.get('job_runtime_minutes', 120) / 60) * a.get('spark_executors', 10) * a.get('spark_executor_memory_gb', 16) for a in applications)

    # Per-application cost breakdown
    app_results = []
    for app in applications:
        hrs = app.get('job_runtime_minutes', 120) / 60
        vcpu_hrs = app.get('spark_executors', 10) * app.get('spark_executor_cores', 4) * hrs
        mem_hrs = app.get('spark_executors', 10) * app.get('spark_executor_memory_gb', 16) * hrs
        share = app.get('job_runtime_minutes', 120) / total_job_minutes if total_job_minutes > 0 else 1 / max(len(applications), 1)

        sls_cost = round(vcpu_hrs * sls_vcpu + mem_hrs * sls_mem, 4)
        ec2_cost = round(share * total_emr_ec2_od, 4)
        ec2_spot_cost = round(share * total_emr_ec2_spot, 4)
        eks_cost = round(share * total_emr_eks_od + vcpu_hrs * eks_vcpu + mem_hrs * eks_mem, 4)
        eks_spot_cost = round(share * total_emr_eks_spot + vcpu_hrs * eks_vcpu + mem_hrs * eks_mem, 4)

        costs = {'EMR Serverless': sls_cost, 'EMR on EC2': ec2_cost, 'EMR on EC2 (Spot)': ec2_spot_cost, 'EMR on EKS': eks_cost, 'EMR on EKS (Spot)': eks_spot_cost}
        recommended = min(costs, key=costs.get)

        app_results.append({
            'application_id': app.get('application_id', 'unknown'),
            'job_name': app.get('job_name', 'Unknown'),
            'job_runtime_minutes': app.get('job_runtime_minutes', 120),
            'cost_analysis': {
                'EMR Serverless': f"${sls_cost} = {vcpu_hrs:.1f} vCPU-hrs × ${sls_vcpu} + {mem_hrs:.1f} GB-hrs × ${sls_mem}",
                'EMR on EC2': f"${ec2_cost} = {share:.1%} share of ${total_emr_ec2_od:.4f} (on-demand)",
                'EMR on EC2 (Spot)': f"${ec2_spot_cost} = {share:.1%} share of ${total_emr_ec2_spot:.4f} (spot)",
                'EMR on EKS': f"${eks_cost} = {share:.1%} share of ${total_emr_eks_od:.4f} + EKS uplift",
                'EMR on EKS (Spot)': f"${eks_spot_cost} = {share:.1%} share of ${total_emr_eks_spot:.4f} + EKS uplift",
            },
            'recommended_deployment': recommended,
            'savings_vs_most_expensive': round(max(costs.values()) - min(costs.values()), 4),
        })

    utilization = round((total_job_minutes / 60 / cluster_runtime_hours * 100), 2) if cluster_runtime_hours > 0 else 0

    # Calculate AWS Glue cost estimate — only when user signals interest in managed/visual features
    glue_estimate = None
    workload_desc = _workflow_data.get('workload_description', '').lower()
    glue_keywords = ['glue', 'visual', 'no-code', 'no code', 'drag and drop', 'managed etl',
                     'data quality', 'crawler', 'bookmark', 'console', 'serverless etl',
                     'all options', 'all deployment', 'compare all', 'include glue']
    show_glue = any(kw in workload_desc for kw in glue_keywords)
    if show_glue:
        try:
            from glue_cost_estimator import estimate_glue_cost
            glue_estimate = estimate_glue_cost(applications, region=region)
        except Exception as e:
            logger.warning(f"Failed to estimate Glue cost: {e}")
    else:
        glue_estimate = {'note': 'AWS Glue pricing not shown. Mention "glue", "visual ETL", "no-code", or "compare all options" to include Glue in the comparison.'}

    # Detect if using real data or defaults
    has_real_cluster_data = bool(timing.get('cluster_runtime_hours')) and cluster.get('instance_count', 0) > 0
    has_real_spark_data = any(a.get('executor_count_method') == 'direct_shs_api' for a in applications) or \
                          any(a.get('workload_characteristics') not in (None, 'mixed', 'batch_processing', 'error_fallback') for a in applications)
    data_quality = 'measured' if (has_real_cluster_data and has_real_spark_data) else 'estimated_defaults'

    pricing_return = {
        'status': 'success',
        'data_quality': data_quality,
        'data_quality_warning': None if data_quality == 'measured' else
            '⚠️ Results use estimated defaults — not real cluster/Spark data. Do NOT cite specific numbers as findings. Re-run with accessible cluster ID and Spark app for accurate analysis.',
        'IMPORTANT': 'Use the EXACT cost numbers from cost_analysis and calculation_breakdown below. Do NOT recalculate or round these figures.',
        'individual_workload_analysis': app_results,
        'aws_glue_estimate': glue_estimate,
        'cluster_context': {
            'cluster_runtime_hours': cluster_runtime_hours,
            'total_instance_hours': round(sum(i['runtime_hours'] for i in instance_details) if instance_details else instance_count * cluster_runtime_hours, 2),
            'utilization_percentage': utilization,
            'spot_instance_percentage': cluster.get('spot_instance_percentage', 0),
            'autoscaling_enabled': cluster.get('autoscaling_enabled', False),
            'estimated_from_spark_data': estimated_cluster,
            'estimated_instance_type': cluster.get('instance_type') if estimated_cluster else None,
            'estimated_instance_count': instance_count if estimated_cluster else None,
        },
        'pricing_rates': rates,
        'pricing_components': {
            'emr_serverless': f"${sls_vcpu}/vCPU-hr + ${sls_mem}/GB-hr",
            'emr_on_ec2': {itype: f"${rates['ec2_rates'].get(itype, 0)}/hr EC2 + ${rates['emr_rates'].get(itype, 0)}/hr EMR" for itype in rates.get('ec2_rates', {})},
            'emr_on_eks': f"${eks_cluster_rate}/hr (EKS cluster) + EC2 + ${eks_vcpu}/vCPU-hr + ${eks_mem}/GB-hr (EMR uplift)",
            'billing_method': 'per-second with per-instance-type rates',
        },
        'optimization_insights': {
            'cluster_utilization': f'{utilization}% — {"very low, ideal for serverless" if utilization < 10 else "moderate"}',
            'bootstrapping_waste': f'{timing.get("bootstrapping_minutes", 0):.1f} min paid but unusable',
        },

        # FINAL EVIDENCE-BASED RECOMMENDATION
        # Weighs factors based on user priorities (default: cost 40%, workload 30%, ops 20%, constraints 10%)
        'recommendation': _build_evidence_based_recommendation(
            app_results, extracted_spark_data, extracted_cluster_data, utilization,
            has_kubernetes_experience=_workflow_data.get('has_kubernetes_experience', False),
            weights=_resolve_weights(_workflow_data.get('preferences')),
        ),

        'analysis_timestamp': datetime.utcnow().isoformat(),
    }

    # Store for report generation (on-demand only)
    _workflow_data['pricing_result'] = pricing_return
    return pricing_return

@mcp.tool()
def generate_report(region: str = 'us-east-1') -> Dict[str, Any]:
    """
    Generate an HTML report from the analysis results. Call ONLY when the user
    explicitly asks for a report (e.g., "generate a report", "save the results").
    Do NOT call this automatically after every analysis.

    Args:
        region: AWS region (for display in report header)
    """
    pricing_result = _workflow_data.get('pricing_result')
    if not pricing_result:
        return {'status': 'error', 'message': 'No pricing data available. Run get_emr_pricing first.'}

    try:
        from report_generator import generate_report as _gen_report
        result = _gen_report(
            pricing_result=pricing_result,
            workload_description=_workflow_data.get('workload_description', ''),
            region=region,
            has_kubernetes_experience=_workflow_data.get('has_kubernetes_experience', False),
        )
        return result
    except Exception as e:
        return {'status': 'error', 'message': f'Failed to generate report: {str(e)}'}


@mcp.tool()
def analyze_glue_job(
    job_name: str,
    region: str = 'us-east-1',
) -> Dict[str, Any]:
    """
    Analyze an existing AWS Glue job and estimate what it would cost on EMR deployment options.

    Fetches the Glue job's most recent successful run, maps the worker config to
    equivalent Spark executor config, and stores it for get_emr_pricing to use.

    Use this when a customer is running on Glue and wants to know if EMR would be cheaper.

    Args:
        job_name: AWS Glue job name
        region: AWS region
    """
    try:
        from glue_cost_estimator import analyze_glue_job as _analyze_glue

        result = _analyze_glue(job_name, region=region)
        if result.get('status') != 'success':
            return result

        # Store in workflow data so get_emr_pricing can use it
        _workflow_data['spark_applications'] = result['spark_applications']
        _workflow_data['cluster_data'] = {
            'timing_analysis': {
                'cluster_runtime_hours': result['primary_run']['glue_config']['execution_time_minutes'] / 60,
            },
            'cluster_analysis': {
                'instance_count': 0,
                'instance_type': 'glue_workers',
            },
            'pattern_analysis': {},
        }
        _workflow_state['spark_analyzed'] = True
        _workflow_state['cluster_analyzed'] = True

        return {
            'status': 'success',
            'NEXT_STEP': 'Call get_emr_pricing() to compare costs across EMR options.',
            'glue_job': job_name,
            'actual_glue_cost': result['actual_glue_cost'],
            'glue_config': result['primary_run']['glue_config'],
            'spark_equivalent': result['primary_run']['spark_equivalent'],
            'note': result['note'],
        }
    except Exception as e:
        return {'status': 'error', 'message': str(e)}


# =============================================================================
# MAIN EXECUTION
# =============================================================================

if __name__ == "__main__":
    # Auto-load knowledge sources at startup
    try:
        logger.info("🚀 Starting Combined EMR Best Practices Advisor MCP Server")
        logger.info("📚 Auto-loading curated knowledge sources...")
        
        load_result = unified_engine.knowledge_processor.load_all_knowledge_sources()
        
        if unified_engine.knowledge_processor.blogs_loaded:
            logger.info("✅ Blog content loaded successfully")
        else:
            logger.warning("⚠️ Blog content loading failed")
        
        if unified_engine.knowledge_processor.pdfs_loaded:
            logger.info("✅ PDF content loaded successfully")
        else:
            logger.warning("⚠️ PDF content loading failed")
        
        if SEMANTIC_SEARCH_AVAILABLE:
            logger.info("✅ Semantic search capabilities available")
        else:
            logger.info("ℹ️ Semantic search not available - using keyword search")
        
        if AWS_AVAILABLE:
            logger.info("✅ AWS SDK available for cluster analysis")
        else:
            logger.warning("⚠️ AWS SDK not available - cluster analysis will be limited")
        
        logger.info("🎯 Combined EMR Advisor ready - use get_emr_deployment_recommendation() for unified insights")
        
    except Exception as e:
        logger.error(f"⚠️ Startup warning: {e}")
        logger.info("Server will continue with limited functionality")
    
    # Run the MCP server
    mcp.run()

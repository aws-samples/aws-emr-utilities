# EMR Serverless Splunk Forwarder

Custom Docker image for [Amazon EMR Serverless](https://docs.aws.amazon.com/emr/latest/EMR-Serverless-UserGuide/emr-serverless.html) that runs the Splunk Universal Forwarder alongside Spark, forwarding driver and executor logs directly to your Splunk infrastructure.

## How it works

A [Spark listener](listener/src/com/emr/splunk/SplunkForwarderListener.java) automatically starts the Splunk Universal Forwarder when your Spark application begins. The forwarder monitors Spark's stdout/stderr log files and forwards them to your Splunk receiver over TCP. **No changes to your Spark application code are required.**

```
EMR Serverless Container
┌──────────────────────────────┐
│  Spark (driver/executor)     │ writes logs to /var/log/spark/user/
│  Splunk UF (background)     │ reads logs, forwards over TCP
└──────────┬───────────────────┘
           │ port 9997
           ▼
   Splunk Receiver / Indexer
```

## Prerequisites

- AWS account with EMR Serverless access
- Docker installed
- Splunk Universal Forwarder RPM ([download here](https://www.splunk.com/en_us/download/universal-forwarder.html) — free, no license required)
- A Splunk receiver (indexer or heavy forwarder) accepting data on port 9997
- VPC with a subnet and security group that can reach your Splunk receiver

## Quick start

### 1. Download the Splunk Universal Forwarder RPM

Download the **Linux x86_64 RPM** (or aarch64 if using ARM) from [splunk.com](https://www.splunk.com/en_us/download/universal-forwarder.html) and place it in this directory:

```bash
cp ~/Downloads/splunkforwarder-*.rpm splunkforwarder.rpm
```

### 2. Build and push the Docker image

```bash
export AWS_ACCOUNT_ID=123456789012
export AWS_REGION=us-east-1

# Create ECR repository
aws ecr create-repository --repository-name emr-serverless-splunk --region $AWS_REGION

# Grant EMR Serverless access to pull the image
aws ecr set-repository-policy --repository-name emr-serverless-splunk --region $AWS_REGION \
  --policy-text '{
    "Version": "2012-10-17",
    "Statement": [{
      "Sid": "EmrServerlessAccess",
      "Effect": "Allow",
      "Principal": {"Service": "emr-serverless.amazonaws.com"},
      "Action": ["ecr:BatchGetImage","ecr:DescribeImages","ecr:GetDownloadUrlForLayer"]
    }]
  }'

# Build and push
docker build -t emr-serverless-splunk .
docker tag emr-serverless-splunk $AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/emr-serverless-splunk:latest
aws ecr get-login-password --region $AWS_REGION | docker login --username AWS --password-stdin $AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com
docker push $AWS_ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/emr-serverless-splunk:latest
```

> **ARM instances:** If building on ARM (Graviton), use the aarch64 Splunk RPM and add `--architecture ARM64` when creating the EMR Serverless application.

### 3. Create an EMR Serverless application

```bash
aws emr-serverless create-application \
  --name my-spark-app \
  --release-label emr-7.1.0 \
  --type SPARK \
  --image-configuration '{"imageUri": "'$AWS_ACCOUNT_ID'.dkr.ecr.'$AWS_REGION'.amazonaws.com/emr-serverless-splunk:latest"}' \
  --network-configuration '{
    "subnetIds": ["subnet-XXXXX"],
    "securityGroupIds": ["sg-XXXXX"]
  }'
```

The **network configuration is required** — without it, the Splunk forwarder cannot reach your Splunk receiver. The security group must allow **outbound TCP to port 9997** (and 8089 if using a deployment server).

### 4. Submit a job

Pass the Splunk configuration as environment variables via Spark conf:

```bash
aws emr-serverless start-job-run \
  --application-id <app-id> \
  --execution-role-arn <role-arn> \
  --job-driver '{
    "sparkSubmit": {
      "entryPoint": "s3://your-bucket/your-job.jar",
      "sparkSubmitParameters": "--class com.example.Main --conf spark.extraListeners=com.emr.splunk.SplunkForwarderListener --conf spark.emr-serverless.driverEnv.SPLUNK_FORWARD_SERVER=splunk.example.com:9997 --conf spark.emr-serverless.driverEnv.SPLUNK_APP_NAME=my-app --conf spark.executorEnv.SPLUNK_FORWARD_SERVER=splunk.example.com:9997 --conf spark.executorEnv.SPLUNK_APP_NAME=my-app"
    }
  }'
```

That's it. Your Spark logs will appear in Splunk.

## Configuration

All configuration is passed via environment variables at job submission time.

| Environment Variable | Required | Default | Description |
|---|---|---|---|
| `SPLUNK_FORWARD_SERVER` | **Yes** | — | Splunk receiver `host:port` (e.g. `splunk.example.com:9997`) |
| `SPLUNK_APP_NAME` | No | `emr-serverless-app` | Application name, used in Splunk host field and sourcetype |
| `SPLUNK_DEPLOYMENT_URI` | No | — | Deployment server hostname (omit to skip deployment client) |
| `SPLUNK_INDEX` | No | `main` | Target Splunk index |

Set env vars for both driver and executors:

```
--conf spark.emr-serverless.driverEnv.SPLUNK_FORWARD_SERVER=splunk.example.com:9997
--conf spark.executorEnv.SPLUNK_FORWARD_SERVER=splunk.example.com:9997
```

## Migrating from EMR on EC2 bootstrap action

If you currently use a bootstrap action to install the Splunk forwarder on EMR on EC2, the migration is straightforward. The same information you pass as bootstrap arguments maps to environment variables:

**EMR on EC2 (bootstrap action):**
```bash
splunkforwarder_installer.sh my-app prod default false true
# Constructs: splunk-deployment-{pod}.monitoring.{context}.internal.example.com:8089
```

**EMR Serverless (environment variables):**
```bash
--conf spark.extraListeners=com.emr.splunk.SplunkForwarderListener \
--conf spark.emr-serverless.driverEnv.SPLUNK_FORWARD_SERVER=splunk-deployment-default.monitoring.prod.internal.example.com:9997 \
--conf spark.emr-serverless.driverEnv.SPLUNK_DEPLOYMENT_URI=splunk-deployment-default.monitoring.prod.internal.example.com \
--conf spark.emr-serverless.driverEnv.SPLUNK_APP_NAME=my-app \
--conf spark.executorEnv.SPLUNK_FORWARD_SERVER=splunk-deployment-default.monitoring.prod.internal.example.com:9997 \
--conf spark.executorEnv.SPLUNK_DEPLOYMENT_URI=splunk-deployment-default.monitoring.prod.internal.example.com \
--conf spark.executorEnv.SPLUNK_APP_NAME=my-app
```

| Bootstrap argument | Environment variable | Example |
|---|---|---|
| `app_name` | `SPLUNK_APP_NAME` | `my-app` |
| `security_context` + `indexing_pod` | `SPLUNK_FORWARD_SERVER` | `splunk-deployment-default.monitoring.prod.internal.example.com:9997` |
| `security_context` + `indexing_pod` | `SPLUNK_DEPLOYMENT_URI` | `splunk-deployment-default.monitoring.prod.internal.example.com` |

> **Note:** The VPC and security group on your EMR Serverless application must allow outbound connectivity to your Splunk deployment server (port 8089) and receiver (port 9997), just as your EMR on EC2 cluster nodes could reach them.

## What gets forwarded

| Log path | Sourcetype |
|---|---|
| `/var/log/spark/user/stderr` | `{APP_NAME}-spark-stderr` |
| `/var/log/spark/user/stdout` | `{APP_NAME}-spark-stdout` |

To monitor additional paths, edit `conf/inputs.conf.template` before building the image.

## Searching in Splunk

```
index=main host=my-app-* sourcetype=my-app-spark-stderr
```

## Architecture

The image contains:

- **Splunk Universal Forwarder** — pre-installed, starts at runtime
- **Spark listener JAR** (`/usr/lib/spark/jars/splunk-listener.jar`) — auto-loaded by Spark when `spark.extraListeners` is set
- **Config templates** — rendered from environment variables by `start-splunk.sh`
- **start-splunk.sh** — called by the listener, renders configs and starts the forwarder

The listener fires on `SparkListenerApplicationStart`, calls `start-splunk.sh`, which renders the config templates and starts `splunkd` as a background process. The forwarder then tails the Spark log files and forwards them over TCP to your Splunk receiver.

## Building the listener JAR from source

The pre-built JAR is included at `listener/splunk-listener.jar`. To rebuild:

```bash
cd listener
chmod +x build.sh
./build.sh
```

Requires Java JDK and Docker (to extract the Spark core JAR from the EMR image).

## Customization

- **Additional log paths:** Edit `conf/inputs.conf.template`
- **SSL/TLS forwarding:** Edit `conf/outputs.conf.template` to add SSL settings
- **Different EMR release:** Change the `FROM` line in the Dockerfile
- **Splunk version:** Download a different RPM version

## Troubleshooting

**Forwarder not starting:** Check `/tmp/splunk-start.log` inside the container. Ensure `SPLUNK_FORWARD_SERVER` is set.

**No data in Splunk:** Verify the security group allows outbound TCP 9997 from the EMR Serverless subnet to your Splunk receiver.

**Short jobs:** The forwarder takes ~8 seconds to start. Very short jobs (< 10s) may complete before all logs are forwarded.

## License

This project is licensed under the MIT License. The Splunk Universal Forwarder is free but subject to [Splunk's license terms](https://www.splunk.com/en_us/legal/splunk-software-license-agreement.html).

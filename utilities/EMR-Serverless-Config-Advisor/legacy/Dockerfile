FROM public.ecr.aws/docker/library/python:3.9-slim

RUN apt-get update && apt-get install -y gcc g++ && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/
RUN pip install --no-cache-dir -r /app/requirements.txt awscli

COPY pipeline_wrapper.py spark_processor.py emr_recommender.py format_to_job_config.py write_to_iceberg.py /app/

WORKDIR /app
ENTRYPOINT ["python3", "pipeline_wrapper.py"]

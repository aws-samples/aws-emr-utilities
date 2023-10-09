from datetime import datetime, timedelta
import boto3
import time,random
from faker import Faker
import logging

# Setup logging
logging.basicConfig(level=logging.INFO)
fake = Faker()
def create_and_wait_for_table(dynamodb, dynamodb_resource, table_name, attribute_definitions, key_schema, provisioned_throughput):
    try:
        existing_tables = dynamodb.list_tables()["TableNames"]
        if table_name in existing_tables:
            logging.info(f"Table {table_name} already exists.")
            return
        dynamodb.create_table(
            TableName=table_name,
            AttributeDefinitions=attribute_definitions,
            KeySchema=key_schema,
            ProvisionedThroughput=provisioned_throughput
        )
        logging.info(f"Table {table_name} created successfully. Waiting for it to be active...")
        table = dynamodb_resource.Table(table_name)
        table.wait_until_exists()
        logging.info(f"Table {table_name} is now active.")

        # Enable PITR
        dynamodb.update_continuous_backups(
            TableName=table_name,
            PointInTimeRecoverySpecification={
                'PointInTimeRecoveryEnabled': True
            }
        )
        logging.info(f"Point-In-Time Recovery has been enabled for {table_name}.")

    except Exception as e:
        logging.error(f"Could not create table {table_name}. Error: {e}")
def populate_table(table, items):
    for item in items:
        try:
            table.put_item(Item=item)
        except Exception as e:
            logging.error(f"Could not insert item {item}. Error: {e}")
    logging.info(f"Populated table {table.name} successfully.")
if __name__ == "__main__":
    dynamodb_client = boto3.client('dynamodb')
    dynamodb_resource = boto3.resource('dynamodb')
    # Create and wait for Inventory table
    create_and_wait_for_table(
        dynamodb_client,
        dynamodb_resource,
        'OnlineCompany-inventory',
        [{"AttributeName": "product_id", "AttributeType": "S"}],
        [{"AttributeName": "product_id", "KeyType": "HASH"}],
        {"ReadCapacityUnits": 1, "WriteCapacityUnits": 1}
    )
    # Create and wait for Customer Orders table
    create_and_wait_for_table(
        dynamodb_client,
        dynamodb_resource,
        'OnlineCompany-customer-orders',
        [
            {"AttributeName": "order_id", "AttributeType": "S"},
            {"AttributeName": "product_id", "AttributeType": "S"}
        ],
        [
            {"AttributeName": "order_id", "KeyType": "HASH"},
            {"AttributeName": "product_id", "KeyType": "RANGE"}
        ],
        {"ReadCapacityUnits": 1, "WriteCapacityUnits": 1}
    )
    # Populate Inventory table
    inventory_table = dynamodb_resource.Table('OnlineCompany-inventory')
    inventory_items = [
        {
            'product_id': str(random.randint(1, 20)),
            'product_name': fake.word(),
            'price': str(fake.pyfloat(positive=True)),
            'quantity': fake.random_int(min=1, max=100),
            'inventory_date': fake.date_this_month(before_today=True, after_today=False).isoformat(),
            'remaining_count': fake.random_int(min=100, max=1000)
        } for _ in range(20)
    ]
    populate_table(inventory_table, inventory_items)
    # Create date within the range of current date -5 to current date +90
    def random_date():
      start_date = datetime.now() - timedelta(days=5)
      end_date = datetime.now() + timedelta(days=90)
      random_date = start_date + (end_date - start_date) * random.random()
      return random_date.date().isoformat()

    # Populate Customer Orders table
    orders_table = dynamodb_resource.Table('OnlineCompany-customer-orders')
    order_items = [
        {
            'order_id': fake.uuid4(),
            'customer_name': fake.name(),
            'product_id': str(random.randint(1, 20)),
            'quantity': fake.random_int(min=1, max=10),
            'order_date': fake.date_this_month(before_today=True, after_today=False).isoformat(),
            'ship_date': random_date(), 
            'customer_pin': fake.random_int(min=1000, max=9999),
            'zipcode': fake.zipcode()
        } for _ in range(10)
    ]
    populate_table(orders_table, order_items)

def export_table_to_s3(dynamodb, dynamodb_resource, table_name, s3_bucket, export_time=None):
    try:
        # Get table ARN
        table = dynamodb_resource.Table(table_name)
        table_arn = table.table_arn

        export_config = {
            "S3Bucket": s3_bucket,
            "S3Prefix": table_name + "/",
            "ExportFormat": "DYNAMODB_JSON"
        }
        if export_time:
            response = dynamodb.export_table_to_point_in_time(
                TableArn=table_arn,
                S3Bucket=s3_bucket,
                S3Prefix=table_name,
                ExportTime=export_time,
                ExportFormat="DYNAMODB_JSON"
            )
        else:
            response = dynamodb.export_table_to_point_in_time(
                TableArn=table_arn,
                S3Bucket=s3_bucket,
                S3Prefix=table_name,
                ExportFormat="DYNAMODB_JSON"
            )
        export_arn = response['ExportDescription']['ExportArn']
        logging.info(f"Initiated export of {table_name} to {s3_bucket}. Export ARN: {response['ExportDescription']['ExportArn']}")
        # Wait for export to complete
        while True:
            export_description = dynamodb.describe_export(ExportArn=export_arn)['ExportDescription']
            status = export_description['ExportStatus']
            if status == 'COMPLETED':
                logging.info(f"Export of {table_name} to {s3_bucket} is complete.")
                break
            elif status == 'FAILED':
                logging.error(f"Export of {table_name} to {s3_bucket} failed.")
                break
            logging.info(f"Waiting for export of {table_name} to complete. Current status: {status}")
            time.sleep(10)  # wait for 10 seconds before checking again

    except Exception as e:
        logging.error(f"Could not export table {table_name} to S3. Error: {e}")
# Full Export Tables Once Created:
if __name__ == "__main__":
    # Export tables to S3
    #export_table_to_s3(dynamodb_client, dynamodb_resource, 'OnlineCompany-inventory', 'your-s3-bucket-name')
    #export_table_to_s3(dynamodb_client, dynamodb_resource, 'OnlineCompany-customer-orders', 'your-s3-bucket-name')
    export_table_to_s3(dynamodb_client, dynamodb_resource, 'OnlineCompany-inventory', 'kp-bda')
    export_table_to_s3(dynamodb_client, dynamodb_resource, 'OnlineCompany-customer-orders', 'kp-bda')

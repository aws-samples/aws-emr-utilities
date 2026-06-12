-- External tables over the synthetic parquet. ${DATA} is the dataset root
-- (s3://... or hdfs://...), substituted by run script via sed.
CREATE DATABASE IF NOT EXISTS communications;
CREATE DATABASE IF NOT EXISTS user;
CREATE DATABASE IF NOT EXISTS metrics_platform;

DROP TABLE IF EXISTS communications.ingestion_clickstream_base;
CREATE EXTERNAL TABLE communications.ingestion_clickstream_base (
  duaid string, event_timestamp bigint, visit_id string,
  event_type string, event_name string,
  eg_account_id string, brand_customer_id string, eg_user_id string,
  event_ts_pst timestamp, local_dtm timestamp,
  funnel_brand string, lob string,
  device_type string, device_os string, domain string, site_name string,
  visit_mktg_code string, email_omni_code string, experience_type string,
  uis_prime_referrer_id string,
  pad_0 string, pad_1 string
) PARTITIONED BY (event_date date) STORED AS PARQUET
LOCATION '${DATA}/communications/ingestion_clickstream_base';
MSCK REPAIR TABLE communications.ingestion_clickstream_base;

DROP TABLE IF EXISTS user.keychain_eg_v3;
CREATE EXTERNAL TABLE user.keychain_eg_v3 (
  keychain_id string,
  expuserid map<string,struct<key_last_visit_date:date>>,
  guid map<string,struct<key_last_visit_date:date>>,
  havid map<string,struct<key_last_visit_date:date>>,
  device_user_agent_id map<string,struct<key_last_visit_date:date>>,
  pad_0 string, pad_1 string, pad_2 string, pad_3 string
) STORED AS PARQUET
LOCATION '${DATA}/user/keychain_eg_v3';

DROP TABLE IF EXISTS metrics_platform.cks_trvlr_visit_msr_v4;
CREATE EXTERNAL TABLE metrics_platform.cks_trvlr_visit_msr_v4 (
  visit_id string, line_of_business string,
  shopping_visit_flag boolean, shopping_cvr_flag boolean, booking_flag boolean,
  pad_0 string
) PARTITIONED BY (visit_date date) STORED AS PARQUET
LOCATION '${DATA}/metrics_platform/cks_trvlr_visit_msr_v4';
MSCK REPAIR TABLE metrics_platform.cks_trvlr_visit_msr_v4;

DROP TABLE IF EXISTS communications.sms_engagement_base;
CREATE EXTERNAL TABLE communications.sms_engagement_base (
  recipient_id string, sms_omni_code string, brand string,
  communication_type string, sfmc_send_timestamp_pst timestamp,
  max_timestamp timestamp, eg_account_id string, pad_0 string
) PARTITIONED BY (sent_date date) STORED AS PARQUET
LOCATION '${DATA}/communications/sms_engagement_base';
MSCK REPAIR TABLE communications.sms_engagement_base;

DROP TABLE IF EXISTS communications.inbox_engagement_base;
CREATE EXTERNAL TABLE communications.inbox_engagement_base (
  recipient_id string, inbox_omni_code string, brand string,
  communication_type string, sent_time timestamp, pad_0 string
) PARTITIONED BY (sent_date date) STORED AS PARQUET
LOCATION '${DATA}/communications/inbox_engagement_base';
MSCK REPAIR TABLE communications.inbox_engagement_base;

DROP TABLE IF EXISTS communications.sms_clickstream_enrichment_repro;
DROP TABLE IF EXISTS communications.inbox_clickstream_enrichment_repro;

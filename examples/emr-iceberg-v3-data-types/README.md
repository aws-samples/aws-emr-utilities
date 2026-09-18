# Apache Iceberg v3 data types in Amazon EMR 8.1

Companion notebook for the AWS Big Data Blog post *"Amazon EMR 8.1 completes support for the Apache
Iceberg v3 data types."*

With **Amazon EMR 8.1**, the [Apache Iceberg v3 data types](https://iceberg.apache.org/spec/) are
complete. This notebook exercises five industry-neutral v3 data-type capabilities end to end —
**default column values**, the **unknown type (`VOID`)**, the **`GEOMETRY`** and **`GEOGRAPHY`**
geospatial types, and **nanosecond-precision timestamps** — grounded in one running example of five
related retail tables that reference each other.

> **Requires Amazon EMR 8.1** (the v3 types are rejected on EMR 7.x). Validated on **EMR Serverless,
> Spark 4.1.1**. Every cell runs top-to-bottom and prints the result it describes; the final query
> joins three of the tables with a point-in-polygon test.

## What it demonstrates

| # | Iceberg v3 capability | Table | What it shows |
|---|---|---|---|
| 1 | Default column values | `orders` | Add `fulfillment_channel` to a table with history — existing rows backfill the default with **no data-file rewrite** |
| 2 | The unknown type (`VOID`) | `customers` | Publish `loyalty_tier` as a stable schema contract before the feature launches |
| 3 | `GEOMETRY` | `delivery_zones` | Planar polygons with an SRID; the type EMR 8.1 spatial functions compute on |
| 4 | `GEOGRAPHY` | `stores` | Canonical point on a spherical earth model |
| 5 | Nanosecond timestamps | `clickstream` | Order high-volume events that tie at microsecond precision |
| — | Bringing it together | (join) | Point-in-polygon query: which store's delivery zone contains each customer |

## Contents

```
Apache_Iceberg_v3_data_types_EMR_8.1.ipynb   # the notebook (config → 5 examples → join → cleanup)
```

## Prerequisites

- An **Amazon EMR Serverless application on EMR 8.1** (Spark). The features also work on Amazon EMR
  on EC2 and Amazon EMR on EKS.
- An EMR Serverless **job execution role** (or cluster instance role) with **AWS Glue Data Catalog**
  and **Amazon S3** access.
- An **S3 bucket** for the Iceberg warehouse (this notebook uses
  `s3://amzn-s3-demo-bucket/iceberg-v3-features/`).

## Quick start

1. Open the notebook on an **EMR Studio** notebook attached to an EMR 8.1 cluster, or on an **EMR
   Serverless interactive** application.
2. In the first code cell, replace `amzn-s3-demo-bucket` with your own bucket. The session config
   sets a Glue-backed catalog named `glue_catalog` and enables geospatial support
   (`spark.sql.geospatial.enabled=true`), which is **off by default** and required for `GEOMETRY` /
   `GEOGRAPHY`.
3. Run the cells top to bottom. Each capability section prints the output it describes.
4. The **Clean up** cell at the end drops the tables and database; delete any remaining warehouse
   objects in Amazon S3 to avoid ongoing charges.

## Notes on EMR 8.1 syntax

A few v3 specifics the notebook relies on:

- Create tables with **`TBLPROPERTIES ('format-version'='3')`** — the v3 types are rejected on v2.
- The unknown type is spelled **`VOID`** in Spark SQL (`UNKNOWN` raises a parser error).
- `GEOMETRY` / `GEOGRAPHY` require a **numeric SRID** (`GEOMETRY(4326)`); a named CRS such as
  `'OGC:CRS84'` is not accepted. Build values with `st_geomfromtext(...)` / `st_geogfromwkb(...)` and
  stamp the SRID with `st_setsrid(..., 4326)` — a bare constructor yields SRID 0.
- The `ST_*` **computation** functions operate on `GEOMETRY`, not `GEOGRAPHY`. Read a `GEOGRAPHY`
  value with `st_srid(...)` and `hex(st_asbinary(...))`, and use `GEOMETRY` for spatial math. Use
  `st_within(point, polygon)` (point-then-polygon order); `st_contains` is not registered.
- Declare nanosecond timestamps with precision 9: `TIMESTAMP_NTZ(9)` or `TIMESTAMP(9)`. There is no
  nanosecond literal — `CAST` a string to the type.

"""One-time account bootstrap: the S3 bucket and execution role `etd` needs.

A brand-new account has neither, which is where most people stall. This creates
exactly two things, both tagged and both removable with `etd teardown
--delete-iam`, and prints the two config lines to paste back.

The policy is scoped to the run's own bucket and to Glue. Glue actions cannot be
usefully scoped to a database that does not exist yet, so the Glue statement
covers the catalog plus databases and tables in this account and region; narrow
it further if your account policy requires it.
"""

from __future__ import annotations

import json

TRUST = {
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Principal": {"Service": "emr-serverless.amazonaws.com"},
        "Action": "sts:AssumeRole",
    }],
}


def policy(bucket: str, region: str, account: str) -> dict:
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "RunBucketReadWrite",
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject",
                           "s3:ListBucket", "s3:GetBucketLocation"],
                "Resource": [f"arn:aws:s3:::{bucket}", f"arn:aws:s3:::{bucket}/*"],
            },
            {
                "Sid": "GlueCatalog",
                "Effect": "Allow",
                "Action": ["glue:GetDatabase", "glue:GetDatabases", "glue:CreateDatabase",
                           "glue:DeleteDatabase", "glue:UpdateDatabase",
                           "glue:GetTable", "glue:GetTables", "glue:CreateTable",
                           "glue:UpdateTable", "glue:DeleteTable",
                           "glue:GetPartition", "glue:GetPartitions", "glue:BatchCreatePartition",
                           "glue:CreatePartition", "glue:UpdatePartition", "glue:DeletePartition",
                           "glue:BatchGetPartition", "glue:BatchDeletePartition",
                           "glue:GetUserDefinedFunctions"],
                "Resource": [
                    f"arn:aws:glue:{region}:{account}:catalog",
                    f"arn:aws:glue:{region}:{account}:database/*",
                    f"arn:aws:glue:{region}:{account}:table/*/*",
                ],
            },
        ],
    }


def bootstrap(factory, spec, create_bucket: bool = True) -> dict:
    """Create the bucket and execution role. Idempotent."""
    s3 = factory.client("s3")
    iam = factory.client("iam")
    out: dict = {}

    if create_bucket:
        try:
            s3.head_bucket(Bucket=spec.bucket)
            print(f"  bucket s3://{spec.bucket} already exists")
        except Exception:  # noqa: BLE001
            kwargs = {"Bucket": spec.bucket}
            if spec.region != "us-east-1":
                # us-east-1 rejects an explicit LocationConstraint.
                kwargs["CreateBucketConfiguration"] = {"LocationConstraint": spec.region}
            s3.create_bucket(**kwargs)
            print(f"  created bucket s3://{spec.bucket}")
        out["bucket"] = spec.bucket

    role_name = f"etd-{spec.name}-execution-role"[:64]
    try:
        existing = iam.get_role(RoleName=role_name)["Role"]
        print(f"  role {role_name} already exists")
        arn = existing["Arn"]
    except Exception:  # noqa: BLE001
        arn = iam.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=json.dumps(TRUST),
            Description=f"EMR Test Drive execution role for run {spec.name}",
            Tags=[{"Key": k, "Value": v} for k, v in spec.resource_tags().items()],
        )["Role"]["Arn"]
        print(f"  created role {arn}")

    iam.put_role_policy(
        RoleName=role_name, PolicyName="etd-access",
        PolicyDocument=json.dumps(policy(spec.bucket, spec.region, spec.account)))
    print(f"  attached inline policy etd-access")
    out["execution_role_arn"] = arn
    out["role_name"] = role_name

    print("\nPaste into your config under run::\n"
          f"  bucket: {spec.bucket}\n"
          f"  execution_role_arn: {arn}\n")
    return out


def delete_iam(factory, spec) -> None:
    iam = factory.client("iam")
    role_name = f"etd-{spec.name}-execution-role"[:64]
    # Remove every inline policy, not just the one bootstrap created: enabling a
    # Lake Formation access mode adds another, and IAM refuses to delete a role
    # that still carries any.
    try:
        for name in iam.list_role_policies(RoleName=role_name).get("PolicyNames", []):
            iam.delete_role_policy(RoleName=role_name, PolicyName=name)
            print(f"  removed inline policy {name}")
    except Exception as exc:  # noqa: BLE001
        print(f"  could not list inline policies: {exc}")
    try:
        for pol in iam.list_attached_role_policies(RoleName=role_name).get("AttachedPolicies", []):
            iam.detach_role_policy(RoleName=role_name, PolicyArn=pol["PolicyArn"])
    except Exception:  # noqa: BLE001
        pass
    try:
        iam.delete_role(RoleName=role_name)
        print(f"  deleted role {role_name}")
    except Exception as exc:  # noqa: BLE001
        print(f"  role not deleted: {exc}")

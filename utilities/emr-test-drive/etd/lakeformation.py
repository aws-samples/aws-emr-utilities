"""Lake Formation test bed for the lf_fta and lf_fgac access modes.

Doing this correctly is most of the work in testing FGAC, and the requirements
are not obvious:

* **A user-defined registration role, not the service-linked role.** The FTA
  documentation is explicit that full table access requires a customer-managed
  role for the registered location.
* **Full-table-access application integration.** FTA needs Lake Formation to
  allow third-party engines to read without IAM session-tag validation
  (`AllowFullTableExternalDataAccess`). Without it, FTA jobs get credentials
  they cannot use.
* **`lakeformation:GetDataAccess` on the job runtime role.** Lake Formation
  permissions alone are not enough; the role also needs the IAM action.
* **FGAC is an application-level switch, FTA is a job-level configuration.**
  They cannot coexist on one application, so each access mode is its own variant
  with its own application.

Everything created here is tagged and removed by `teardown`. The data-lake
settings change is read-modify-write: it adds an administrator and flips one
flag rather than replacing the account's settings, because clobbering them in a
shared account would break other people's access.
"""

from __future__ import annotations

import json
import time

REGISTRATION_TRUST = {
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Principal": {"Service": "lakeformation.amazonaws.com"},
        "Action": "sts:AssumeRole",
    }],
}

# Permissions the job runtime role needs on the catalog objects. Reads need
# SELECT; writes and deletes need ALL (SUPER) per the FTA documentation.
DB_PERMISSIONS = ["CREATE_TABLE", "ALTER", "DROP", "DESCRIBE"]
TABLE_PERMISSIONS = ["SELECT", "INSERT", "DELETE", "ALTER", "DROP", "DESCRIBE"]


def registration_role_name(spec) -> str:
    return f"etd-{spec.name}-lf-registration"[:64]


def _registration_policy(bucket: str, prefix: str) -> dict:
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
                "Resource": [f"arn:aws:s3:::{bucket}/{prefix}/*"],
            },
            {
                "Effect": "Allow",
                "Action": ["s3:ListBucket", "s3:GetBucketLocation"],
                "Resource": [f"arn:aws:s3:::{bucket}"],
            },
        ],
    }


def ensure_registration_role(factory, spec) -> str:
    iam = factory.client("iam")
    name = registration_role_name(spec)
    data_prefix = f"{spec.prefix}/{spec.name}/data"
    try:
        arn = iam.get_role(RoleName=name)["Role"]["Arn"]
        print(f"  lf: registration role {name} exists")
    except Exception:  # noqa: BLE001
        arn = iam.create_role(
            RoleName=name,
            AssumeRolePolicyDocument=json.dumps(REGISTRATION_TRUST),
            Description=f"Lake Formation registration role for EMR Test Drive run {spec.name}",
            Tags=[{"Key": k, "Value": v} for k, v in spec.resource_tags().items()],
        )["Role"]["Arn"]
        print(f"  lf: created registration role {arn}")
    iam.put_role_policy(RoleName=name, PolicyName="etd-lf-registration",
                        PolicyDocument=json.dumps(_registration_policy(spec.bucket, data_prefix)))
    # Lake Formation must be able to pass this role when it vends credentials.
    iam.put_role_policy(
        RoleName=name, PolicyName="etd-lf-passrole",
        PolicyDocument=json.dumps({
            "Version": "2012-10-17",
            "Statement": [{"Effect": "Allow", "Action": "iam:PassRole", "Resource": arn}],
        }))
    return arn


def ensure_runtime_role_lf_permissions(factory, spec) -> None:
    """Add lakeformation:GetDataAccess and glue access to the job runtime role."""
    iam = factory.client("iam")
    role = spec.execution_role_arn.split("/")[-1]
    doc = {
        "Version": "2012-10-17",
        "Statement": [
            {"Sid": "LakeFormationAccess", "Effect": "Allow",
             "Action": ["lakeformation:GetDataAccess",
                        "lakeformation:GetTemporaryGlueTableCredentials",
                        "lakeformation:GetTemporaryGluePartitionCredentials"],
             "Resource": "*"},
            {"Sid": "GlueForLakeFormation", "Effect": "Allow",
             "Action": ["glue:Get*", "glue:Create*", "glue:Update*", "glue:Delete*",
                        "glue:BatchGet*", "glue:BatchCreate*", "glue:BatchDelete*"],
             "Resource": "*"},
        ],
    }
    try:
        iam.put_role_policy(RoleName=role, PolicyName="etd-lakeformation",
                            PolicyDocument=json.dumps(doc))
        print(f"  lf: granted lakeformation:GetDataAccess to {role}")
    except Exception as exc:  # noqa: BLE001
        print(f"  lf: could not update runtime role {role}: {exc}")


def enable_full_table_access(factory, spec, caller_arn: str) -> None:
    """Read-modify-write the data-lake settings.

    Adds the caller and the job runtime role as data-lake administrators and
    turns on full-table external data access, which FTA requires. Never replaces
    the existing settings wholesale — in a shared account that would revoke
    other people's administration.
    """
    lf = factory.client("lakeformation")
    try:
        settings = lf.get_data_lake_settings()["DataLakeSettings"]
    except Exception as exc:  # noqa: BLE001
        print(f"  lf: cannot read data-lake settings: {exc}")
        return

    admins = settings.get("DataLakeAdmins") or []
    have = {a["DataLakePrincipalIdentifier"] for a in admins}
    added = []
    for arn in (caller_arn, spec.execution_role_arn):
        if arn and arn not in have:
            admins.append({"DataLakePrincipalIdentifier": arn})
            added.append(arn)

    changed = bool(added)
    if not settings.get("AllowFullTableExternalDataAccess"):
        changed = True

    if not changed:
        print("  lf: data-lake settings already correct")
        return

    settings["DataLakeAdmins"] = admins
    settings["AllowFullTableExternalDataAccess"] = True
    # Keep these as-is if present; only send fields the API accepts.
    payload = {k: settings[k] for k in (
        "DataLakeAdmins", "ReadOnlyAdmins", "CreateDatabaseDefaultPermissions",
        "CreateTableDefaultPermissions", "Parameters", "TrustedResourceOwners",
        "AllowExternalDataFiltering", "AllowFullTableExternalDataAccess",
        "ExternalDataFilteringAllowList", "AuthorizedSessionTagValueList",
    ) if k in settings}
    try:
        lf.put_data_lake_settings(DataLakeSettings=payload)
        print(f"  lf: AllowFullTableExternalDataAccess=true"
              + (f", added {len(added)} administrator(s)" if added else ""))
    except Exception as exc:  # noqa: BLE001
        print(f"  lf: could not update data-lake settings: {exc}")


def register_location(factory, spec, role_arn: str) -> None:
    lf = factory.client("lakeformation")
    arn = f"arn:aws:s3:::{spec.bucket}/{spec.prefix}/{spec.name}/data"
    try:
        existing = lf.describe_resource(ResourceArn=arn)["ResourceInfo"]
        if existing.get("RoleArn") != role_arn:
            lf.update_resource(ResourceArn=arn, RoleArn=role_arn)
            print(f"  lf: updated registration role on {arn}")
        else:
            print(f"  lf: {arn} already registered")
        return
    except Exception:  # noqa: BLE001
        pass
    for attempt in range(5):
        try:
            lf.register_resource(ResourceArn=arn, RoleArn=role_arn, UseServiceLinkedRole=False)
            print(f"  lf: registered {arn}")
            return
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            if "AlreadyExistsException" in msg:
                print(f"  lf: {arn} already registered")
                return
            # IAM role propagation is eventually consistent.
            if attempt < 4 and ("not authorized" in msg or "Invalid" in msg or "assume" in msg):
                time.sleep(6)
                continue
            print(f"  lf: register_resource failed: {exc}")
            return


def grant(factory, spec) -> None:
    """Grant the job runtime role what it needs on the database and its tables."""
    lf = factory.client("lakeformation")
    principal = {"DataLakePrincipalIdentifier": spec.execution_role_arn}
    grants = [
        ("database", {"Database": {"Name": spec.database}}, DB_PERMISSIONS),
        ("tables", {"Table": {"DatabaseName": spec.database, "TableWildcard": {}}},
         TABLE_PERMISSIONS),
        ("data location",
         {"DataLocation": {"ResourceArn":
                           f"arn:aws:s3:::{spec.bucket}/{spec.prefix}/{spec.name}/data"}},
         ["DATA_LOCATION_ACCESS"]),
    ]
    for label, resource, perms in grants:
        try:
            lf.grant_permissions(Principal=principal, Resource=resource, Permissions=perms)
            print(f"  lf: granted {', '.join(perms)} on {label}")
        except Exception as exc:  # noqa: BLE001
            if "already exists" in str(exc).lower():
                print(f"  lf: grant on {label} already present")
            else:
                print(f"  lf: grant on {label} failed: {exc}")


# ---------------------------------------------------------------- data filters
#
# Granting whole-table SELECT exercises the FGAC code path -- record server,
# second driver, credential vending -- but enforces nothing, so it cannot tell a
# working deployment from one where filtering silently does not apply. These
# filters give each variant a *known* expected result, so the harness can assert
# that what came back is what the filter permits.
#
# Column filters are expressed as an inclusion list because Lake Formation
# validates excluded columns against the table schema at grant time, and the
# harness adds a column mid-sequence (ALTER TABLE ADD COLUMNS).

FILTER_SPECS = {
    # name suffix        row expression            included columns
    "row":    ("category = 'c1'",                  None),
    "column": ("TRUE",                             ["fact_id", "dim_id", "category"]),
    "cell":   ("category = 'c1'",                  ["fact_id", "category"]),
}


def filter_name(spec, table: str, kind: str) -> str:
    return f"etd_{spec.name}_{table}_{kind}".replace("-", "_")[:255]


def data_filter_plan(spec, tables: list[str]) -> list[dict]:
    """The filters this run expects, as plain data.

    Returned to the caller and written into the run manifest so the job harness
    and the report agree on what should have been enforced.
    """
    plan = []
    for table in tables:
        for kind, (row_expr, cols) in FILTER_SPECS.items():
            plan.append({
                "kind": kind,
                "name": filter_name(spec, table, kind),
                "table": table,
                "row_filter": row_expr,
                "column_names": cols,
            })
    return plan


def create_data_filters(factory, spec, tables: list[str]) -> list[dict]:
    """Create the data cell filters and grant SELECT on each to the job role.

    Idempotent: an existing filter of the same name is deleted and recreated, so
    a changed expression does not silently keep the old one.
    """
    lf = factory.client("lakeformation")
    principal = {"DataLakePrincipalIdentifier": spec.execution_role_arn}
    plan = data_filter_plan(spec, tables)

    for f in plan:
        body = {
            "TableCatalogId": spec.account,
            "DatabaseName": spec.database,
            "TableName": f["table"],
            "Name": f["name"],
            "RowFilter": {"FilterExpression": f["row_filter"]},
        }
        if f["column_names"]:
            body["ColumnNames"] = f["column_names"]
        else:
            body["ColumnWildcard"] = {"ExcludedColumnNames": []}

        try:
            lf.delete_data_cells_filter(
                TableCatalogId=spec.account, DatabaseName=spec.database,
                TableName=f["table"], Name=f["name"])
        except Exception:  # noqa: BLE001
            pass                       # did not exist, which is the normal case

        try:
            lf.create_data_cells_filter(TableData=body)
            f["created"] = True
            print(f"  lf: created data filter {f['name']} ({f['kind']})")
        except Exception as exc:  # noqa: BLE001
            f["created"] = False
            f["error"] = str(exc)[:300]
            print(f"  lf: data filter {f['name']} failed: {f['error']}")
            continue

        try:
            lf.grant_permissions(
                Principal=principal,
                Resource={"DataCellsFilter": {
                    "TableCatalogId": spec.account,
                    "DatabaseName": spec.database,
                    "TableName": f["table"],
                    "Name": f["name"]}},
                Permissions=["SELECT"])
            f["granted"] = True
            print(f"  lf: granted SELECT on data filter {f['name']}")
        except Exception as exc:  # noqa: BLE001
            f["granted"] = False
            f["error"] = str(exc)[:300]
            print(f"  lf: grant on data filter {f['name']} failed: {f['error']}")
    return plan


def delete_data_filters(factory, spec, tables: list[str]) -> None:
    lf = factory.client("lakeformation")
    for f in data_filter_plan(spec, tables):
        try:
            lf.delete_data_cells_filter(
                TableCatalogId=spec.account, DatabaseName=spec.database,
                TableName=f["table"], Name=f["name"])
            print(f"  lf: deleted data filter {f['name']}")
        except Exception:  # noqa: BLE001
            pass


def setup(factory, spec, caller_arn: str) -> dict:
    """Full Lake Formation test bed. Idempotent."""
    print("\n== lake formation test bed ==")
    role_arn = ensure_registration_role(factory, spec)
    ensure_runtime_role_lf_permissions(factory, spec)
    enable_full_table_access(factory, spec, caller_arn)
    time.sleep(8)   # let the registration role propagate before LF assumes it
    register_location(factory, spec, role_arn)
    grant(factory, spec)
    # Data filters are created only for the FGAC path: full table access vends
    # whole-table credentials by definition and cannot enforce a cell filter.
    filters = []
    if any(v.access_mode == "lf_fgac" for v in spec.variants):
        filters = create_data_filters(factory, spec, ["fact"])
    return {"registration_role_arn": role_arn, "data_filters": filters}


def teardown(factory, spec) -> None:
    lf = factory.client("lakeformation")
    iam = factory.client("iam")
    delete_data_filters(factory, spec, ["fact"])
    arn = f"arn:aws:s3:::{spec.bucket}/{spec.prefix}/{spec.name}/data"
    try:
        lf.deregister_resource(ResourceArn=arn)
        print(f"  lf: deregistered {arn}")
    except Exception as exc:  # noqa: BLE001
        print(f"  lf: deregister skipped: {str(exc)[:120]}")
    name = registration_role_name(spec)
    for policy in ("etd-lf-registration", "etd-lf-passrole"):
        try:
            iam.delete_role_policy(RoleName=name, PolicyName=policy)
        except Exception:  # noqa: BLE001
            pass
    try:
        iam.delete_role(RoleName=name)
        print(f"  lf: deleted registration role {name}")
    except Exception as exc:  # noqa: BLE001
        print(f"  lf: registration role not deleted: {str(exc)[:120]}")

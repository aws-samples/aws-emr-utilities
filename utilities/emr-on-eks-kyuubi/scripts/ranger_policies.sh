#!/bin/bash

#===============================================================================
# Configurations
#===============================================================================
RANGER_URL="http://ranger-0.ranger-headless.kyuubi.svc.cluster.local:6080"
RANGER_CREDENTIALS="admin:Rangeradmin1!"

EKS_NS="kyuubi"
KYUUBI_URL="kyuubi-rest.kyuubi.svc.cluster.local"
LDAP_SERVICE_USER="admin"
LDAP_SERVICE_PASSWD="admin"
HIVE_SERVICE_NAME="hivedev"

#===============================================================================
# Hive Service Definition
#===============================================================================
curl -u $RANGER_CREDENTIALS -X POST  \
-H "Accept: application/json" \
-H "Content-Type: application/json" \
-k "$RANGER_URL/service/public/v2/api/service" \
-d @<(cat <<EOF
{
  "type": "hive",
  "name": "$HIVE_SERVICE_NAME",
  "displayName": "kyuubi-hive",
  "description": "Amazon EMR Hive Policies",
  "configs": {
    "commonNameForCertificate": "*",
    "jdbc.driverClassName": "org.apache.hive.jdbc.HiveDriver",
    "jdbc.url": "jdbc:hive2://$KYUUBI_URL:10099/",
    "username": "$LDAP_SERVICE_USER",
    "password": "$LDAP_SERVICE_PASSWD",
    "ranger.plugin.audit.filters": "[{'accessResult':'DENIED','isAudited':true},{'actions':['METADATA OPERATION'],'isAudited':false},{'users':['hive','hue'],'actions':['SHOW_ROLES'],'isAudited':false}]"
  }
}
EOF
)

#===============================================================================
# Analyst - Policies
#===============================================================================
curl -u $RANGER_CREDENTIALS -X POST  \
-H "Accept: application/json" \
-H "Content-Type: application/json" \
-k "$RANGER_URL/service/public/v2/api/policy/apply" \
-d @<(cat <<EOF
{
  "service": "$HIVE_SERVICE_NAME",
  "name": "secure_datalake - customer only",
  "policyType": 0,
  "policyPriority": 0,
  "description": "secure_datalake - customer only",
  "isAuditEnabled": true,
  "resources": {
    "database": {
      "values": [
        "secure_datalake"
      ],
      "isExcludes": false,
      "isRecursive": false
    },
    "column": {
      "values": [
        "*"
      ],
      "isExcludes": false,
      "isRecursive": false
    },
    "table": {
      "values": [
        "customer"
      ],
      "isExcludes": false,
      "isRecursive": false
    }
  },
  "policyItems": [
    {
      "accesses": [
        {
          "type": "select",
          "isAllowed": true
        }
      ],
      "users": ["analyst"],
      "groups": [],
      "roles": [],
      "conditions": [],
      "delegateAdmin": false
    }
  ],
  "denyPolicyItems": [],
  "allowExceptions": [],
  "denyExceptions": [],
  "dataMaskPolicyItems": [],
  "rowFilterPolicyItems": [],
  "serviceType": "hive",
  "options": {},
  "validitySchedules": [],
  "policyLabels": [
    "Analytics"
  ],
  "zoneName": "",
  "isDenyAllElse": false,
  "guid": "aa3d0f5e-f98b-46d9-bf8c-6940f8970139",
  "isEnabled": true
}
EOF
)

#===============================================================================
# Analyst - Data Masking
#===============================================================================
curl -u $RANGER_CREDENTIALS -X POST  \
-H "Accept: application/json" \
-H "Content-Type: application/json" \
-k "$RANGER_URL/service/public/v2/api/policy/apply" \
-d @<(cat <<EOF
{
  "service": "$HIVE_SERVICE_NAME",
  "name": "PII customer.mail masking",
  "policyType": 1,
  "policyPriority": 0,
  "description": "",
  "isAuditEnabled": true,
  "resources": {
    "database": {
      "values": [
        "secure_datalake"
      ],
      "isExcludes": false,
      "isRecursive": false
    },
    "column": {
      "values": [
        "mail"
      ],
      "isExcludes": false,
      "isRecursive": false
    },
    "table": {
      "values": [
        "customer"
      ],
      "isExcludes": false,
      "isRecursive": false
    }
  },
  "policyItems": [],
  "denyPolicyItems": [],
  "allowExceptions": [],
  "denyExceptions": [],
  "dataMaskPolicyItems": [
    {
      "dataMaskInfo": {
        "dataMaskType": "MASK_HASH",
        "valueExpr": ""
      },
      "accesses": [
        {
          "type": "select",
          "isAllowed": true
        }
      ],
      "users": ["analyst"],
      "groups": [],
      "roles": [],
      "conditions": [],
      "delegateAdmin": false
    }
  ],
  "rowFilterPolicyItems": [],
  "serviceType": "hive",
  "options": {},
  "validitySchedules": [],
  "policyLabels": [
    "Analytics"
  ],
  "zoneName": "",
  "isDenyAllElse": false,
  "id": 13,
  "guid": "183b17c4-6515-452b-8795-64f3fc8aed23",
  "isEnabled": true,
  "version": 2
}
EOF
)

#!/usr/bin/env bash
#===============================================================================
#!# build_kyuubi_deployment.sh - Create k8s deployment objects for Kyuubi
#!#
#!#  version         1.0
#!#  author          ripani
#!#
#===============================================================================
#?#
#?# usage: ./build_kyuubi_deployment.sh <RANGER_URL> <RANGER_SERVICE> <LDAP_URL> <LDAP_USER_ATTR> <LDAP_BASE_USER_DN>
#?#        ./build_kyuubi_deployment.sh "http://ip-10-0-11-233.eu-west-1.compute.internal:6080" "hivedev" "ldap://ip-10-0-11-233.eu-west-1.compute.internal:389" "uid" "ou=People,dc=hadoop,dc=local" "kyuubi" "spark-kyuubi" "917161301401.dkr.ecr.eu-west-1.amazonaws.com/kyuubi/emr-6.6.0:latest" "s3://ripani.dub/warehouse"
#?#
#?#   RANGER_URL                 Url of Ranger Admin Server
#?#   RANGER_SERVICE_DEF         Existing Ranger service definition for Hive
#?#   LDAP_URL                   Ldap url for user authentication
#?#   LDAP_USER_ATTR             Ldap user search attribute
#?#   LDAP_BASE_USER_DN          Ldap base DC for user search
#?#   EKS_NS                     k8s existing namespace to deploy kyuubi
#?#   EKS_SA                     k8s Service Account used by kyuubi pods
#?#   KYUUBI_IMAGE               Custom Kyuubi image on ECR repository
#?#   S3_WAREHOUSE               S3 warehouse location (important only if using Iceberg)
#?#
#===============================================================================
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"

function usage() {
  [ "$*" ] && echo "$0: $*"
  sed -n '/^#?#/,/^$/s/^#?# \{0,1\}//p' "$0"
  exit -1
}

[[ $# -ne 9 ]] && echo "error: wrong parameters" && usage

RANGER_URL="$1"
RANGER_SERVICE_DEF="$2"

LDAP_URL="$3"
LDAP_USER_ATTR="$4"
LDAP_BASE_USER_DN="$5"

EKS_NS="$6"
EKS_SA="$7"
KYUUBI_IMAGE="$8"
S3_WAREHOUSE="$9"

BUILD_PATH="$DIR/deployments"

#===============================================================================
# Kyuubi deployment
#===============================================================================
mkdir -p $BUILD_PATH
cat << EOF > $BUILD_PATH/kyuubi.yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: kyuubi-conf
data:
  kyuubi-defaults.conf: |

    # Spark overrides
    ## cluster mode requires to specify a DFS file system (S3, HDFS, ..)
    ## we pass all the required jars using the spark.jars conf
    spark.submit.deployMode                                       cluster
    spark.kubernetes.file.upload.path                             /tmp
    spark.driver.memory                                           5g
    spark.driver.maxResultSize                                    4g
    spark.executor.cores                                          2
    spark.executor.memory                                         5g

    # Spark Dynamic Allocation
    spark.dynamicAllocation.enabled                               true
    spark.dynamicAllocation.shuffleTracking.enabled               true
    spark.dynamicAllocation.initialExecutors                      1
    spark.dynamicAllocation.maxExecutors                          10

    # Container Images
    spark.kubernetes.container.image                              $KYUUBI_IMAGE
    spark.kubernetes.driver.container.image                       $KYUUBI_IMAGE
    spark.kubernetes.executor.container.image                     $KYUUBI_IMAGE
    spark.kubernetes.driver.pod.name                              spark-kyuubi-driver
    spark.kubernetes.driver.podTemplateContainerName              spark-kyuubi-driver
    spark.kubernetes.driver.podTemplateFile                       /etc/kyuubi/conf/driver-template.yaml

    # pod authentications (IRSA)
    spark.kubernetes.namespace                                    $EKS_NS
    spark.kubernetes.authenticate.driver.serviceAccountName       $EKS_SA
    spark.kubernetes.authenticate.executor.serviceAccountName     $EKS_SA
    spark.kubernetes.authenticate.serviceAccountName              $EKS_SA

    ## Kyuubi
    kyuubi.ha.addresses                                           zk-cs:2181
    kyuubi.ha.zookeeper.quorum                                    zk-cs:2181
    kyuubi.frontend.connection.url.use.hostname                   false
    kyuubi.engine.connection.url.use.hostname                     false
    kyuubi.frontend.protocols                                     THRIFT_BINARY,THRIFT_HTTP,REST

    # Authentication (client users)
    kyuubi.authentication                                         LDAP
    kyuubi.authentication.ldap.url                                $LDAP_URL
    kyuubi.authentication.ldap.base.dn                            $LDAP_BASE_USER_DN
    kyuubi.authentication.ldap.guidKey                            $LDAP_USER_ATTR


    # Hive Data Catalog (Glue or External)
    spark.hadoop.hive.metastore.client.factory.class              com.amazonaws.glue.catalog.metastore.AWSGlueDataCatalogHiveClientFactory

    # Hudi, Delta, Iceberg configs
    spark.jars                                                    local:///usr/lib/hudi/hudi-spark-bundle.jar,local:///usr/lib/delta/delta-core.jar,local:///usr/lib/delta/delta-storage.jar,local:///usr/share/aws/iceberg/lib/iceberg-spark3-runtime.jar,local:///usr/lib/kyuubi/externals/engines/spark/kyuubi-spark-sql.jar

    # Additional extensions for (Delta, Iceberg, Ranger)
    spark.sql.extensions                                          io.delta.sql.DeltaSparkSessionExtension, org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions, org.apache.kyuubi.plugin.spark.authz.ranger.RangerSparkExtension

    # Delta
    spark.sql.catalog.spark_catalog                               org.apache.spark.sql.delta.catalog.DeltaCatalog

    # Iceberg
    spark.sql.catalog.iceberg                                     org.apache.iceberg.spark.SparkCatalog
    spark.sql.catalog.iceberg.catalog-impl                        org.apache.iceberg.aws.glue.GlueCatalog
    spark.sql.catalog.iceberg.io-impl                             org.apache.iceberg.aws.s3.S3FileIO
    spark.sql.catalog.iceberg.warehouse                           $S3_WAREHOUSE

  log4j2.properties: |
    # Set everything to be logged to the file
    rootLogger.level = info
    rootLogger.appenderRef.stdout.ref = STDOUT
    # Console Appender
    appender.console.type = Console
    appender.console.name = STDOUT
    appender.console.target = SYSTEM_OUT
    appender.console.layout.type = PatternLayout
    appender.console.layout.pattern = %d{HH:mm:ss.SSS} %p %c: %m%n
    appender.console.filter.1.type = Filters
    appender.console.filter.1.a.type = ThresholdFilter
    appender.console.filter.1.a.level = info
    # SPARK-34128: Suppress undesirable TTransportException warnings, due to THRIFT-4805
    # modified to also remove errors related to ELB port checks
    appender.console.filter.1.b.type = RegexFilter
    appender.console.filter.1.b.regex = .*occurred during processing of message.*
    appender.console.filter.1.b.onMatch = deny
    appender.console.filter.1.b.onMismatch = neutral
    # Set the default kyuubi-ctl log level to WARN. When running the kyuubi-ctl, the
    # log level for this class is used to overwrite the root logger's log level.
    logger.ctl.name = org.apache.kyuubi.ctl.ServiceControlCli
    logger.ctl.level = error
    # Kyuubi BeeLine
    logger.beeline.name = org.apache.hive.beeline.KyuubiBeeLine
    logger.beeline.level = error

  driver-template.yaml: |
    apiVersion: v1
    Kind: Pod
    metadata:
      labels:
        template-label-key: kyuubi-template
    spec:
      containers:
        - name: spark-kyuubi-driver
          volumeMounts:
            - name: ranger-spark-conf
              mountPath: /etc/spark/conf/ranger-spark-audit.xml
              subPath: ranger-spark-audit.xml
            - name: ranger-spark-conf
              mountPath: /etc/spark/conf/ranger-spark-security.xml
              subPath: ranger-spark-security.xml
      volumes:
        - name: ranger-spark-conf
          configMap:
            name: ranger-spark-conf
            items:
              - key: ranger-spark-audit.xml
                path: ranger-spark-audit.xml
              - key: ranger-spark-security.xml
                path: ranger-spark-security.xml

---
apiVersion: v1
kind: ConfigMap
data:
metadata:
  name: ranger-spark-conf
data:
  ranger-spark-security.xml: |
    <configuration>
        <property>
            <name>ranger.plugin.spark.policy.rest.url</name>
            <value>$RANGER_URL</value>
        </property>
        <property>
            <name>ranger.plugin.spark.service.name</name>
            <value>$RANGER_SERVICE_DEF</value>
        </property>
        <property>
            <name>ranger.plugin.spark.policy.cache.dir</name>
            <value>ranger/policycache</value>
        </property>
        <property>
            <name>ranger.plugin.spark.policy.pollIntervalMs</name>
            <value>5000</value>
        </property>
        <property>
            <name>ranger.plugin.spark.policy.source.impl</name>
            <value>org.apache.ranger.admin.client.RangerAdminRESTClient</value>
        </property>
    </configuration>
  ranger-spark-audit.xml: |
    <configuration>
        <property>
            <name>xasecure.audit.is.enabled</name>
            <value>false</value>
        </property>
        <property>
            <name>xasecure.audit.destination.db</name>
            <value>false</value>
        </property>
        <property>
            <name>xasecure.audit.destination.db.jdbc.driver</name>
            <value>com.mysql.jdbc.Driver</value>
        </property>
        <property>
            <name>xasecure.audit.destination.db.jdbc.url</name>
            <value>jdbc:mysql://MYSQL/ranger</value>
        </property>
        <property>
            <name>xasecure.audit.destination.db.password</name>
            <value>rangeradmin</value>
        </property>
        <property>
            <name>xasecure.audit.destination.db.user</name>
            <value>rangeradmin</value>
        </property>
    </configuration>

---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: $EKS_SA-role
  namespace: $EKS_NS
rules:
- apiGroups: [""]
  resources: ["persistentvolumeclaims"]
  verbs:  ["create", "delete", "list"]
- apiGroups: [""]
  resources: ["services", "configmaps", "serviceaccounts", "pods"]
  verbs: ["get", "list", "describe", "create", "edit", "delete", "annotate", "patch", "label", "watch"]
- apiGroups: ["rbac.authorization.k8s.io"]
  resources: ["roles", "rolebindings"]
  verbs: ["get", "list", "describe", "create", "edit", "delete", "annotate", "patch", "label"]

---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: $EKS_SA-role-binding
subjects:
- kind: ServiceAccount
  name: $EKS_SA
  namespace: $EKS_NS
roleRef:
  kind: ClusterRole
  name: $EKS_SA-role
  apiGroup: rbac.authorization.k8s.io

---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: kyuubi-deployment
  labels:
    app.kubernetes.io/name: kyuubi-server
    app: kyuubi-server
spec:
  replicas: 2
  selector:
    matchLabels:
      app: kyuubi-server
  template:
    metadata:
      labels:
        app: kyuubi-server
    spec:
      serviceAccountName: $EKS_SA
      containers:
        - name: kyuubi-server
          image: $KYUUBI_IMAGE
          imagePullPolicy: Always
          env:
            - name: KYUUBI_JAVA_OPTS
              value: -Dkyuubi.frontend.bind.host=0.0.0.0
          ports:
            - name: thrift-binary
              containerPort: 10009
            - name: thrift-http
              containerPort: 10010
          volumeMounts:
            - name: kyuubi-conf
              mountPath: /etc/kyuubi/conf/kyuubi-defaults.conf
              subPath: kyuubi-defaults.conf
            - name: kyuubi-conf
              mountPath: /etc/kyuubi/conf/log4j2.properties
              subPath: log4j2.properties
            - name: kyuubi-conf
              mountPath: /etc/kyuubi/conf/driver-template.yaml
              subPath: driver-template.yaml
            - name: ranger-spark-conf
              mountPath: /etc/spark/conf/ranger-spark-audit.xml
              subPath: ranger-spark-audit.xml
            - name: ranger-spark-conf
              mountPath: /etc/spark/conf/ranger-spark-security.xml
              subPath: ranger-spark-security.xml
      volumes:
        - name: kyuubi-conf
          configMap:
            name: kyuubi-conf
            items:
              - key: kyuubi-defaults.conf
                path: kyuubi-defaults.conf
              - key: log4j2.properties
                path: log4j2.properties
              - key: driver-template.yaml
                path: driver-template.yaml
        - name: ranger-spark-conf
          configMap:
            name: ranger-spark-conf
            items:
              - key: ranger-spark-audit.xml
                path: ranger-spark-audit.xml
              - key: ranger-spark-security.xml
                path: ranger-spark-security.xml

---
apiVersion: v1
kind: Service
metadata:
  name: kyuubi-svc
spec:
  selector:
    app: kyuubi-server
  ports:
  - name: thrift-binary
    protocol: TCP
    port: 10009
    targetPort: thrift-binary
  - name: thrift-http
    protocol: TCP
    port: 10010
    targetPort: thrift-http
---
apiVersion: v1
kind: Service
metadata:
  name: kyuubi-balancer
spec:
  type: LoadBalancer
  selector:
    app: kyuubi-server
  ports:
  - name: thrift-binary
    protocol: TCP
    port: 10009
    targetPort: thrift-binary
  - name: thrift-http
    protocol: TCP
    port: 10010
    targetPort: thrift-http
EOF

#===============================================================================
# Kyuubi deployment
#===============================================================================
cat << EOF > $BUILD_PATH/zookeeper.yaml
apiVersion: v1
kind: Service
metadata:
  name: zk-hs
  labels:
    app: zk
spec:
  ports:
  - port: 2888
    name: server
  - port: 3888
    name: leader-election
  clusterIP: None
  selector:
    app: zk
---
apiVersion: v1
kind: Service
metadata:
  name: zk-cs
  labels:
    app: zk
spec:
  ports:
  - port: 2181
    name: client
  selector:
    app: zk
---
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: zk-pdb
spec:
  selector:
    matchLabels:
      app: zk
  maxUnavailable: 1
---
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: zk
spec:
  selector:
    matchLabels:
      app: zk
  serviceName: zk-hs
  replicas: 3
  updateStrategy:
    type: RollingUpdate
  podManagementPolicy: OrderedReady
  template:
    metadata:
      labels:
        app: zk
    spec:
      affinity:
        podAntiAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
            - labelSelector:
                matchExpressions:
                  - key: "app"
                    operator: In
                    values:
                    - zk
              topologyKey: "kubernetes.io/hostname"
      containers:
      - name: kubernetes-zookeeper
        imagePullPolicy: Always
        image: "k8s.gcr.io/kubernetes-zookeeper:1.0-3.4.10"
        resources:
          requests:
            memory: "1Gi"
            cpu: "0.5"
        ports:
        - containerPort: 2181
          name: client
        - containerPort: 2888
          name: server
        - containerPort: 3888
          name: leader-election
        command:
        - sh
        - -c
        - "start-zookeeper \
          --servers=3 \
          --data_dir=/var/lib/zookeeper/data \
          --data_log_dir=/var/lib/zookeeper/data/log \
          --conf_dir=/opt/zookeeper/conf \
          --client_port=2181 \
          --election_port=3888 \
          --server_port=2888 \
          --tick_time=2000 \
          --init_limit=10 \
          --sync_limit=5 \
          --heap=512M \
          --max_client_cnxns=60 \
          --snap_retain_count=3 \
          --purge_interval=12 \
          --max_session_timeout=40000 \
          --min_session_timeout=4000 \
          --log_level=INFO"
        readinessProbe:
          exec:
            command:
            - sh
            - -c
            - "zookeeper-ready 2181"
          initialDelaySeconds: 10
          timeoutSeconds: 5
        livenessProbe:
          exec:
            command:
            - sh
            - -c
            - "zookeeper-ready 2181"
          initialDelaySeconds: 10
          timeoutSeconds: 5
        volumeMounts:
        - name: datadir
          mountPath: /var/lib/zookeeper
      securityContext:
        runAsUser: 1000
        fsGroup: 1000
  volumeClaimTemplates:
  - metadata:
      name: datadir
    spec:
      accessModes: [ "ReadWriteOnce" ]
      resources:
        requests:
          storage: 10Gi

EOF

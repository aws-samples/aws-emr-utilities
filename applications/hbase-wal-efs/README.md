#HBase Multi-AZ WAL on EFS

This application provides demo code to save HBase WAL to multi-AZ via Amazon EFS.

Follow steps below to apply the jar to an Amazon EMR 6.1 cluster

1. Create EFS

    Set Provisioned Throughput according to your traffic.   
    Note down the EFS File system ID which will be used later

2. Compile the Java project and upload walefs-0.1-SNAPSHOT.jar to your s3 bucket

3. Create bootstrap action scripts and upload to your s3 bucket

   mount_efs_6.1.sh
    
    ```
    #!/bin/bash
    set -ex
    cd /home/hadoop
    aws s3 cp s3://**<s3-path-to-jar>**/walefs-0.1-SNAPSHOT.jar ~/
    aws s3 cp s3://**<s3-path-to-script>**/cpnewjar-6.1.sh ~/cpnewjar.sh
    
    sudo yum -y install amazon-efs-utils
    sudo mkdir /walefs
    sudo mount -t efs **<efs-file-system-id>** /walefs/
    sudo sh -c 'echo N > /sys/module/nfs/parameters/nfs4_disable_idmapping'
    sudo nfsidmap -c
    sudo systemctl restart rpcidmapd.service
    sudo useradd hbase
    sudo chown hbase:hbase -R /walefs
    ls -ld /walefs
    
    chmod 755 ~/cpnewjar.sh
    nohup ~/cpnewjar.sh &
    ```
    
   cpnewjar-6.1.sh
    
    ```
    #!/bin/bash
    set -ex
    cd /home/hadoop
    
    DIR1=/usr/lib/hbase/
    DIR2=/usr/lib/hbase/lib/
    SRC=~/walefs-0.1-SNAPSHOT.jar
    while true
    do
        if [[ -d "$DIR1" && -d "$DIR2" ]]; then
            sleep 3
            echo "files exists."
            sudo cp $SRC $DIR1
            sudo cp $SRC $DIR2
            break
        fi
        sleep 3
        
    done
    ```

4. Create EMR Cluster

- HBase storage select S3 
- Enter Configuration as below

    ```
    [
        {
            "classification": "hbase-site",
            "properties": {
                "hbase.wal.provider": "multiwal",
                "hbase.wal.regiongrouping.numgroups": "30",
                "hbase.wal.dir": "efs:///walefs",
                "hbase.wal.hsync": "false",
                "hbase.regionserver.hlog.slowsync.ms": "1000",
                "hbase.wal.efs.fdsync": "true",
                "fs.efs.impl": "org.apache.hadoop.fs.EfsFileSystem"
            }
        }
    ]
    ```
   
- Add a Bootstrap Action to use mount_efs_6.1.sh

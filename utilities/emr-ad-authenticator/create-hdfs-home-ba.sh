#!/bin/bash

sudo tee /usr/bin/create-hdfs-user-home-dir <<"EOF"
#!/bin/bash

if [[ "$(whoami)" != "root" ]]; then
        exit 1
fi

if [[ -z "$SUDO_USER" ]]; then
        username="root"
else
        username="$SUDO_USER"
fi

home_dir=$(sudo -u $username -H bash -c 'echo $HOME')
hdfs_dir=""

if [[ "$username" == "root" ]]; then
        hdfs_dir="/root"
else
        hdfs_dir="/user/$username"
fi

if ! which hdfs >/dev/null 2>&1; then
    echo "WARN: HDFS is not yet running. $hdfs_dir NOT created."
        exit 0
fi

hdfs dfs -ls -d $hdfs_dir > /dev/null 2>&1
lsout=$(echo $?)
if [[ "$lsout" != "0" ]]; then
      #echo file does not exists
      sudo -u hdfs kinit -kt /etc/hdfs.keytab hdfs/$(hostname -f) > /dev/null 2>&1 && \
      sudo -u hdfs hdfs dfs -mkdir $hdfs_dir && \
      emr_ad_auth.sh $username:$username $hdfs_dir

        if [ "$?" = "0" ]; then
                echo "HDFS user home directory created"
                #sed -i '/sudo \/usr\/bin\/create-hdfs-user-home-dir/d' $home_dir/.bashrc
                sudo -u $username touch $home_dir/.hdfs_home_created
        else
                echo "Directory not created"
        fi
fi

EOF

sudo chmod 755 /usr/bin/create-hdfs-user-home-dir

sudo tee -a /etc/sudoers <<"EOF"

ALL  ALL=(root)  NOPASSWD: /usr/bin/create-hdfs-user-home-dir

EOF

sudo tee -a /etc/skel/.bashrc <<"EOF"

if [ ! -e $HOME/.hdfs_home_created ]; then
    sudo /usr/bin/create-hdfs-user-home-dir
fi

EOF
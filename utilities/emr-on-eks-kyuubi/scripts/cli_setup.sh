echo "==============================================="
echo "  install CLI tools on Linux ......"
echo "==============================================="

# Install eksctl on cloudshell
curl -s --location "https://github.com/weaveworks/eksctl/releases/latest/download/eksctl_$(uname -s)_amd64.tar.gz" | tar xz -C /tmp
sudo mv -v /tmp/eksctl /usr/local/bin
echo eksctl version is $(eksctl version)

# Install the latest kubectl on cloudshell
curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl"
sudo install -o root -g root -m 0755 kubectl /usr/local/bin/kubectl
kubectl version --short --client

# Install helm on cloudshell
curl -s https://get.helm.sh/helm-v3.6.3-linux-amd64.tar.gz | tar xz -C ./ &&
    sudo mv linux-amd64/helm /usr/local/bin/helm &&
    rm -r linux-amd64
echo helm cli version is $(helm version --short)
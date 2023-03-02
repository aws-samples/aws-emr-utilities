# Deploying HDFS using kind
All scripts listed here are intended to be run from the kubernetes/hdfs folder

First follow the [instructions here](../../docs/kind-deployment.md) to provision and configure a local Kubernetes cluster, using [kind](https://kind.sigs.k8s.io/) (Kubernetes IN Docker), that the HDFS Helm Chart can be deployed on.

## Deploying Helm charts

```bash
export HADOOP_VERSION=${HADOOP_VERSION:-3.2.1}

helm install hdfs . \
  --set hdfs.namenode.tag=${HADOOP_VERSION} \
  --set hdfs.datanode.tag=${HADOOP_VERSION} \
  --set hdfs.shell.tag=${HADOOP_VERSION}

helm test hdfs
```

## Accessing Web UI (via `kubectl port-forward`)

```
kubectl port-forward svc/hdfs-namenodes 9870:9870
```

Then browse to: http://localhost:9870


## Accessing Web UI (via [Nginx Ingress Controller](https://github.com/kubernetes/ingress-nginx))

Register the FQDNs for each component in DNS e.g.
```bash
echo "127.0.0.1 hdfs.k8s.local" | sudo tee -a /etc/hosts
```

Update the HDFS deployment to route ingress based on FQDNs:
```bash
helm upgrade hdfs . -f ./values-host-based-ingress.yaml --reuse-values
```

Set up port forwarding to the nginx ingress controller:
```bash
sudo KUBECONFIG=$HOME/.kube/config kubectl port-forward -n ingress-nginx svc/ingress-nginx 80:80
```

Then browse to: http://hdfs.k8s.local

# Run CENTRA10 on AWS

This path uses **Amazon EKS with three EC2 L4 GPU workers**. A/B/C use Kubernetes and KEDA; D uses SkyServe on the same EKS cluster. `infra: k8s/<context>` is intentional: AWS supplies the Kubernetes infrastructure. Direct SkyServe-on-EC2 would be a different deployment and is not supported by this experiment runner.

No AWS resources are created by rendering, planning, or running local tests. Commands below that create clusters, node groups, EFS, or add-ons change your AWS account and can incur charges. Choose a region, budget, and supported EKS version before running them. These templates have not been deployed against a live account.

## 1. Account and workstation

Install AWS CLI v2, `eksctl`, `kubectl`, Helm, Python, and `skypilot[kubernetes]`. Authenticate using your organization's AWS SSO/profile or an appropriate IAM role; never put access keys in this repository. Verify the intended account with `aws sts get-caller-identity`.

Choose a region with G6 capacity and check the EC2 **Running On-Demand G and VT instances** quota. Three `g6.2xlarge` workers require 24 G-family vCPUs. There is also a CPU system node and, for final measurements, a CPU benchmark host. We use On-Demand and fixed worker capacity during measurement so instance interruptions and node provisioning do not become extra experimental variables. Quota approval does not guarantee available capacity.

The template uses `g6.2xlarge` (one L4 per worker) for more host RAM than `g6.xlarge`; GPU count remains one per replica. Verify instance specifications, regional pricing, EKS version support, and all quotas before creation. Include EKS, CPU/GPU instances, EBS, EFS, NAT gateway, and data transfer in the budget. AWS budget alerts are not spending caps.

For a short A smoke test you can operate from your laptop without a GPU. For D and final latency measurements, use a CPU Linux EC2 benchmark host in the cluster VPC: SkyServe will expose **private pod IPs**. Give that host Kubernetes access through an IAM role/access entry, and allow its security group to reach the experiment's pod/controller ports (vLLM 8000 and the SkyServe endpoint port). Use SSM or restricted SSH for access. Do not expose unauthenticated vLLM/Prometheus to the public internet. Home-to-AWS API tunnels are for smoke testing, not final latency claims.

## 2. EKS and GPU availability

```bash
cp aws/eks.example.yaml aws/eks.local.yaml
# Edit region, supported Kubernetes version, and operator public IP /32.
# Set GPU desiredCapacity to 1 for the initial smoke test, 3 for full experiments.
eksctl create cluster -f aws/eks.local.yaml --dry-run
# The following command creates paid AWS infrastructure:
eksctl create cluster -f aws/eks.local.yaml
kubectl config current-context
kubectl get nodes -L centra10-role,skypilot.co/accelerator
kubectl get nodes -o custom-columns='NAME:.metadata.name,GPU:.status.allocatable.nvidia\.com/gpu'
```

Keep the CPU system node for monitoring and controllers. GPU nodes use private networking and AL2023 accelerated AMIs. Verify the NVIDIA device plugin is installed and exactly one GPU is allocatable on each GPU worker. `eksctl` supports automatic installation; if your version did not install it, follow the NVIDIA plugin instructions below rather than installing two copies. The template's GPU labels match the AWS experiment profile and SkyPilot. Do not enable GPU time-slicing or node autoscaling during the controlled experiment.

## 3. Shared model cache with EFS

EBS/gp3 is not a substitute for the cross-node ReadWriteMany model cache.

1. Create an **encrypted Regional EFS filesystem in the EKS VPC**. Create a mount target in every worker subnet/AZ in use. Allow TCP 2049 on its security group **from the worker node security groups only**. Keep its filesystem ID for the StorageClass.
2. Install the EFS CSI add-on with its IAM role. With the cluster's OIDC provider enabled by the template, use the documented IRSA setup:

```bash
eksctl create iamserviceaccount --cluster centra10 --region YOUR_REGION \
  --namespace kube-system --name efs-csi-controller-sa \
  --role-name Centra10EFSCSIDriverRole \
  --attach-policy-arn arn:aws:iam::aws:policy/service-role/AmazonEFSCSIDriverPolicy \
  --role-only --approve
aws iam get-role --role-name Centra10EFSCSIDriverRole --query Role.Arn --output text
aws eks create-addon --cluster-name centra10 --region YOUR_REGION \
  --addon-name aws-efs-csi-driver --service-account-role-arn ROLE_ARN_FROM_PREVIOUS_COMMAND
```

3. Copy `aws/efs-storageclass.example.yaml` to `aws/efs-storageclass.local.yaml`, replace `fileSystemId`, and apply it. Wait for the EFS add-on to be healthy. The StorageClass creates access points with root ownership for the current root-running vLLM image; revisit UID/GID settings if you change that image's user.

```bash
kubectl apply -f aws/efs-storageclass.local.yaml
kubectl -n kube-system get pods -l app=efs-csi-controller
```

The Retain policy preserves model data. EFS's PVC size request is not a hard filesystem storage quota. Mounts must work on all three workers before experimentation.

## 4. Monitoring and KEDA

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo add kedacore https://kedacore.github.io/charts
helm repo update
# Choose and record chart versions once, then retain them for all runs.
helm upgrade --install monitoring prometheus-community/kube-prometheus-stack \
  --version PROMETHEUS_CHART_VERSION -n monitoring --create-namespace \
  -f aws/monitoring-values.yaml --wait --timeout 10m
helm upgrade --install keda kedacore/keda --version KEDA_CHART_VERSION \
  -n keda --create-namespace --wait --timeout 10m
helm list -A
```

The supplied values enable discovery of monitors across namespaces and use ephemeral Prometheus storage for short sessions. Export results before restarting/deleting monitoring; use persistent storage if you need server-side retention across sessions. Verify the Prometheus Service name matches `prometheus_internal` in the profile. Keep chart versions alongside `results/versions.json`.

## 5. Render and warm one GPU

```bash
cp aws/config.example.json config.local.json
# Set kube_context to the EXACT kubectl context, plus model revision and vLLM version.
python3 -m experiments.render --config config.local.json
kubectl apply -f generated/storage.yaml
kubectl apply -f generated/download.yaml
kubectl -n centra10 wait --for=condition=complete job/download-model --timeout=1800s
kubectl apply -f generated/serving.yaml
kubectl -n centra10 rollout status deployment/vllm --timeout=1800s
kubectl apply -f generated/monitor.yaml
kubectl apply -f generated/proxy.yaml
kubectl -n centra10 rollout status deployment/vllm-proxy --timeout=180s
```

All manual `kubectl`/Helm commands use your current context: confirm it first. The Python runner and generated SkyPilot tasks explicitly use `kube_context`, including cleanup and metric pod snapshots.

For the smoke test, keep these two commands running in separate terminals:

```bash
kubectl -n centra10 port-forward service/vllm-proxy 8000:8000
kubectl -n monitoring port-forward service/monitoring-kube-prometheus-prometheus 9090:9090
```

The proxy forwards streaming responses and opens a fresh upstream connection per request, allowing the Kubernetes Service to distribute new requests to ready replicas. Forwarding the proxy is different from forwarding `service/vllm`, which pins the benchmark to a single serving pod. Confirm per-pod request distribution once multiple replicas are ready. The proxy and API tunnel can still become bottlenecks; record and validate the load path before final measurements.

```bash
curl http://127.0.0.1:8000/v1/models
python3 -m experiments.run --config config.local.json --smoke
python3 -m experiments.run --config config.local.json --smoke --execute
python3 -m experiments.analyze results/smoke --output results/smoke-analysis
```

Smoke mode uses only A, six short requests at 0.2 req/s, and a separate results directory. It still performs warm-up, telemetry collection, and serving-pod cleanup. It neither proves saturation nor validates autoscaling. D's placeholder settings need not be filled for this A-only check. Do not put smoke outputs in the final 48-run dataset.

## 6. SkyServe and full experiment

On the CPU benchmark host in the VPC, configure AWS/Kubernetes access and install the pinned vLLM benchmark release plus SkyPilot. Copy `aws/skypilot.example.yaml` to `aws/skypilot.local.yaml`, replace both context placeholders, and point `SKYPILOT_CONFIG` at its absolute path:

```bash
export SKYPILOT_CONFIG=/absolute/path/to/CENTRA10/aws/skypilot.local.yaml
sky check kubernetes
```

This config uses pod-IP endpoints and an EKS-hosted SkyServe controller, avoiding public cloud load balancers. Both the CPU host and SkyServe controller must reach the replica pod IPs. Keep the same config active for the walkthrough, runner, and cleanup. Complete the SkyPilot walkthrough in the main README; it identifies actual SkyServe pod labels for `sky_pod_selector`, a PodMonitor, and `sky_queries`. Those labels are intentionally not guessed. Verify telemetry is restricted to this service's GPU replicas, excluding its CPU controller. Do not start the full matrix with placeholder D queries.

For final A–C traffic, use `http://<vllm-proxy-pod-private-IP>:8000` from the VPC benchmark host to avoid the Kubernetes API tunnel in the load path; obtain its address with `kubectl -n centra10 get pods -l app=vllm-proxy -o wide` and set `k8s_url`. Keep Prometheus's local port-forward for metric queries. A proxy restart changes its pod IP; update configuration before restarting a failed run. Confirm the benchmark client is not CPU/network limited. Keep the same A–C load path for all measured rounds.

Before full runs, set the worker group to three and wait for all nodes/GPUs to be ready:

```bash
eksctl scale nodegroup --cluster centra10 --region YOUR_REGION --name gpu --nodes 3 --nodes-min 0 --nodes-max 3
```

Calibrate thresholds, obtain checkpoint evidence, and run the mixed first round before continuing the 48-run matrix, as described in the main README. Keep the same instance type and node count across A–D. The runner scales pods; it does not scale or terminate EC2 workers.

## 7. Stop charges and preserve evidence

After collecting results, stop SkyServe/walkthrough clusters and remove the KEDA scaler before scaling serving to zero (main README cleanup). Then reduce the GPU node group:

```bash
eksctl scale nodegroup --cluster centra10 --region YOUR_REGION --name gpu --nodes 0 --nodes-min 0 --nodes-max 3
```

This terminates GPU workers; do it only after runs finish. **Pod cleanup alone does not stop EC2 charges.** The CPU nodes, benchmark host, EKS control plane, NAT, storage, and any load balancers can still incur charges.

For final teardown, archive results and version/configuration evidence outside the cluster, stop SkyPilot resources, and remove any Kubernetes LoadBalancer services before deleting the cluster with `eksctl delete cluster -f aws/eks.local.yaml --wait`. EFS mount targets in the cluster VPC may block VPC deletion: explicitly remove those mount targets first after stopping consumers. EFS, retained access points/data, IAM roles, and the independently created benchmark EC2 host are not all owned by eksctl; inventory and remove or deliberately retain them separately. Do not delete the model filesystem until you decide its data is expendable. Verify the AWS console for leftovers.

GPU-hours in the paper measure allocated pod GPU capacity. They are **not the AWS invoice**: three preallocated GPU workers are billed even when the experiment uses one replica. Report measured capacity and cloud spend separately.

References: [eksctl GPU support](https://docs.aws.amazon.com/eks/latest/eksctl/gpu-support.html), [EFS CSI setup](https://docs.aws.amazon.com/eks/latest/userguide/efs-csi.html), [NVIDIA device plugin](https://github.com/NVIDIA/k8s-device-plugin), [SkyPilot Kubernetes configuration](https://docs.skypilot.ai/en/latest/reference/config.html).

"""Validate deployment artifacts without credentials or a Kubernetes cluster.

Downloads checksum-verified tools and charts into the chosen output directory.
Only runs eksctl schema, helm lint/template, and kubeconform.
"""
import argparse
import hashlib
import json
import os
import platform
import subprocess
import tarfile
from pathlib import Path
from urllib.request import urlopen

import yaml
import jsonschema

from experiments.config import load
from experiments.render import manifests


ROOT = Path(__file__).resolve().parents[1]


class ChartLoader(yaml.SafeLoader):
    pass


# Kubernetes YAML allows a bare '=' enum value in Prometheus CRDs.
ChartLoader.add_constructor("tag:yaml.org,2002:value", lambda loader, node: loader.construct_scalar(node))


def download(url, target):
    if not target.exists():
        with urlopen(url, timeout=120) as response:
            target.write_bytes(response.read())
    return target


def verify(path, expected):
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        raise ValueError(f"Checksum mismatch: {path}; remove the corrupt download and retry")


def tool(folder, name, version, arch):
    if name == "helm":
        archive = f"helm-v{version}-linux-{arch}.tar.gz"
        base = "https://get.helm.sh/"
        checksum = archive + ".sha256sum"
        member = f"linux-{arch}/helm"
    elif name == "eksctl":
        archive = f"eksctl_Linux_{arch}.tar.gz"
        base = f"https://github.com/eksctl-io/eksctl/releases/download/v{version}/"
        checksum, member = "eksctl_checksums.txt", "eksctl"
    else:
        archive = f"kubeconform-linux-{arch}.tar.gz"
        base = f"https://github.com/yannh/kubeconform/releases/download/v{version}/"
        checksum, member = "CHECKSUMS", "kubeconform"
    location = folder / f"{name}-{version}"
    location.mkdir(parents=True, exist_ok=True)
    packed = download(base + archive, location / archive)
    sums = download(base + checksum, location / checksum).read_text()
    expected = next(line.split()[0] for line in sums.splitlines() if line.split()[-1].lstrip("*") == archive)
    verify(packed, expected)
    binary = location / name
    # Extract only the named executable; never unpack arbitrary archive paths.
    with tarfile.open(packed) as source:
        binary.write_bytes(source.extractfile(member).read())
    binary.chmod(0o755)
    return str(binary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "generated/validation")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    pins = json.loads((ROOT / "versions.json").read_text())
    arch = {"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine())
    if platform.system() != "Linux" or arch is None:
        parser.error("Run deployment validation on Linux amd64/arm64 (including CloudShell/WSL)")
    tools = {name: tool(output, name, pins[name], arch) for name in ("helm", "eksctl", "kubeconform")}
    env = {k: v for k, v in os.environ.items() if not k.startswith("AWS_")}
    env.update(AWS_EC2_METADATA_DISABLED="true", AWS_CONFIG_FILE="/dev/null",
               AWS_SHARED_CREDENTIALS_FILE="/dev/null", KUBECONFIG="/dev/null")

    def check(argv, filename):
        print("Checking:", " ".join(argv), flush=True)
        with (output / filename).open("w") as log:
            subprocess.run(argv, stdout=log, stderr=subprocess.STDOUT, env=env, check=True, timeout=300)

    eks = yaml.safe_load((ROOT / "aws/eks.example.yaml").read_text())
    eks["metadata"]["region"] = "us-east-1"
    eks["vpc"]["publicAccessCIDRs"] = ["203.0.113.1/32"]
    (output / "eks.yaml").write_text(yaml.safe_dump(eks))
    check([tools["eksctl"], "utils", "schema"], "eks-schema.json")
    jsonschema.validate(eks, json.loads((output / "eks-schema.json").read_text()))

    config = load(ROOT / "aws/config.example.json")
    documents = [doc for docs in manifests(config).values() for doc in docs if "apiVersion" in doc]
    documents.append(yaml.safe_load((ROOT / "aws/efs-storageclass.example.yaml").read_text()))
    (output / "experiment.yaml").write_text(yaml.safe_dump_all(documents))
    schema_dir = output / "schemas"
    schema_dir.mkdir(exist_ok=True)
    # The default kubeconform schema collection omits CRD definitions themselves.
    swagger = download(f"https://raw.githubusercontent.com/kubernetes/kubernetes/v{pins['kubernetes']}.0/api/openapi-spec/swagger.json",
                       output / "kubernetes-openapi.json")
    definitions = json.loads(swagger.read_text())["definitions"]
    crd_schema = dict(definitions["io.k8s.apiextensions-apiserver.pkg.apis.apiextensions.v1.CustomResourceDefinition"])
    crd_schema["definitions"] = definitions
    crd_dir = schema_dir / "apiextensions.k8s.io"
    crd_dir.mkdir(exist_ok=True)
    (crd_dir / "customresourcedefinition_v1.json").write_text(json.dumps(crd_schema))
    bundles = [str(output / "experiment.yaml")]
    for name, chart in pins["charts"].items():
        archive = download(chart["url"], output / f"{name}-{chart['version']}.tgz")
        verify(archive, chart["sha256"])
        values = ["-f", str(ROOT / "aws/monitoring-values.yaml")] if name == "monitoring" else []
        check([tools["helm"], "lint", str(archive), "--kube-version", pins["kubernetes"], *values], f"{name}-lint.log")
        check([tools["helm"], "template", name, str(archive), "--namespace", "monitoring" if name == "monitoring" else "keda",
               "--kube-version", pins["kubernetes"], "--include-crds", *values], f"{name}.yaml")
        bundles.append(str(output / f"{name}.yaml"))
        for doc in yaml.load_all((output / f"{name}.yaml").read_text(), Loader=ChartLoader):
            if doc and doc.get("kind") == "CustomResourceDefinition":
                spec = doc["spec"]
                group = schema_dir / spec["group"]
                group.mkdir(exist_ok=True)
                for version in spec["versions"]:
                    path = group / f"{spec['names']['kind'].lower()}_{version['name']}.json"
                    path.write_text(json.dumps(version["schema"]["openAPIV3Schema"]))
    check([tools["kubeconform"], "-strict", "-summary", "-kubernetes-version", pins["kubernetes"] + ".0",
           "-schema-location", "default", "-schema-location",
           str(schema_dir) + "/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json", *bundles], "schemas.log")
    print(f"Deployment checks passed. Logs and rendered charts: {output}")
    print("No AWS resources created. These are schema/render checks, not live API or GPU checks.")


if __name__ == "__main__":
    main()

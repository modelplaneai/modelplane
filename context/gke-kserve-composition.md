# GKE + KServe Composition Design

Nic Cope, March 2026

---

## Background

The Modelplane PRD positions InferenceEnvironment as an inference-focused
abstraction that layers onto an existing Kubernetes cluster. It explicitly does
not provision the cluster — that's a separate concern with a separate lifecycle.
The PRD also calls out "example compositions showing end-to-end cluster +
InferenceEnvironment provisioning for common clouds" as in-scope for v0.1.

This document designs those example compositions for GKE. The central question:
should the GKE cluster and the KServe stack be one XR or two?

I recommend two: `GKECluster` and `KServeStack`. The rest of this document
explains why, defines both XRDs, and sketches the Python composition functions
using Upbound's Pydantic MR models.

---

## Why two XRs

The one-XR case has appeal. A single `GKEKServeCluster` that provisions GKE and
installs KServe is fewer moving parts — one XRD, one function, no cross-XR
wiring. For a demo, simplicity matters.

But the two concerns have different lifecycles, different failure modes, and
different rates of change. A GKE cluster changes when the platform team resizes
node pools, upgrades Kubernetes versions, or adjusts networking. The KServe
stack changes when KServe releases a new version, when Envoy Gateway needs a
config tweak, or when a new inference backend lands in v0.2. Coupling them means
a KServe upgrade triggers a reconciliation that touches GKE infrastructure, and
a node pool resize touches the KServe installation. That's the kind of blast
radius that makes platform teams nervous.

The separation also pays off when Modelplane adds backends. The PRD's v0.2
roadmap includes KubeAI. With two XRs, adding KubeAI means creating a
`KubeAIStack` XR and its function — `GKECluster` doesn't change at all. With
one XR, you need a whole new `GKEKubeAICluster`, duplicating the GKE
provisioning logic.

And `GKECluster` is useful beyond inference. Platform teams running Crossplane
already provision GKE clusters for other workloads. A well-designed `GKECluster`
XR is a reusable building block. Bundling it with KServe makes it
inference-specific.

The cost is wiring. `GKECluster` outputs a ProviderConfig; `KServeStack`
consumes it. The InferenceEnvironment composition function handles this — it
composes both XRs and threads the ProviderConfig reference between them. This is
a common Crossplane pattern and one Python composition functions handle
naturally.

---

## How the pieces fit together

The PRD's `function-modelplane-env` currently does two things: install the KServe
dependency chain on a cluster, and configure environment-level resources
(LLMInferenceServiceConfig, LocalModelNodeGroup, RBAC). With two XRs, the KServe
installation moves into `KServeStack`'s own function, and `function-modelplane-env`
composes a `KServeStack` XR instead of raw Helm releases.

For the end-to-end GKE story, `function-modelplane-env` also composes a
`GKECluster` XR — but only when the InferenceEnvironment's Composition is the
GKE-specific variant. The InferenceEnvironment XRD stays cloud-agnostic. Multiple
Compositions implement it: one for existing clusters (current PRD design), one for
GKE, one for EKS. The platform team selects the Composition via
`spec.crossplane.compositionRef`.

```
InferenceEnvironment (XR)
│
├─ Composition: "inferenceenvironment-existing-cluster"
│  └─ function-modelplane-env
│     └─ Composes: KServeStack XR (takes providerConfigRef from spec)
│
├─ Composition: "inferenceenvironment-gke"
│  └─ function-modelplane-env-gke
│     ├─ Composes: GKECluster XR
│     ├─ Composes: KServeStack XR (wired to GKECluster's ProviderConfig)
│     └─ Composes: environment-level K8s Objects (config, cache, RBAC)
│
└─ Composition: "inferenceenvironment-eks" (future)
   └─ function-modelplane-env-eks
      ├─ Composes: EKSCluster XR
      └─ Composes: KServeStack XR
```

The GKE-specific Composition reads cluster provisioning parameters from an
`spec.cluster.provision` section on the InferenceEnvironment XRD — GCP project,
region, GPU type, node count. These fields are optional; the existing-cluster
Composition ignores them. This is a minor addition to the PRD's XRD, scoped to
`spec.cluster.provision` so the inference-focused fields don't get cluttered.

---

## GKECluster XR

### XRD

```yaml
apiVersion: apiextensions.crossplane.io/v2
kind: CompositeResourceDefinition
metadata:
  name: gkeclusters.infrastructure.modelplane.ai
spec:
  scope: Cluster
  group: infrastructure.modelplane.ai
  names:
    kind: GKECluster
    plural: gkeclusters
  versions:
  - name: v1alpha1
    served: true
    referenceable: true
    schema:
      openAPIV3Schema:
        type: object
        properties:
          spec:
            type: object
            required: [project, region]
            properties:
              project:
                type: string
                description: GCP project ID.
              region:
                type: string
                description: GCP region (e.g. us-central1).
              kubernetesVersion:
                type: string
                default: "1.31"
                description: GKE cluster version.
              networking:
                type: object
                properties:
                  podCidr:
                    type: string
                    default: "10.1.0.0/16"
                  serviceCidr:
                    type: string
                    default: "10.2.0.0/16"
                  nodeCidr:
                    type: string
                    default: "10.0.0.0/24"
              nodePool:
                type: object
                required: [machineType, acceleratorType]
                properties:
                  machineType:
                    type: string
                    description: >-
                      GCE machine type (e.g. a2-highgpu-8g, g2-standard-48).
                  diskSizeGb:
                    type: integer
                    default: 200
                  acceleratorType:
                    type: string
                    description: >-
                      GPU accelerator type
                      (e.g. nvidia-tesla-a100, nvidia-h100-80gb).
                  acceleratorCount:
                    type: integer
                    default: 1
                    description: GPUs per node.
                  nodeCount:
                    type: integer
                    default: 1
                    description: Initial node count.
                  minNodeCount:
                    type: integer
                    default: 0
                  maxNodeCount:
                    type: integer
                    default: 8
          status:
            type: object
            properties:
              providerConfigRef:
                type: object
                properties:
                  name:
                    type: string
                    description: >-
                      Name of the provider-kubernetes and provider-helm
                      ProviderConfig targeting this cluster.
              clusterEndpoint:
                type: string
              clusterCaCertificate:
                type: string
```

The key output is `status.providerConfigRef.name` — the ProviderConfig that
downstream XRs (KServeStack, InferenceEnvironment's K8s Objects) use to target
this cluster.

### What it composes

The function creates six provider-gcp managed resources:

1. **Network** — a dedicated VPC for the cluster.
2. **Subnetwork** — with secondary ranges for pods and services.
3. **Cluster** — a GKE Standard cluster with the default node pool removed.
   Autopilot isn't suitable because it doesn't give fine-grained GPU node pool
   control.
4. **NodePool** — GPU-accelerated nodes with autoscaling, taints, and driver
   installation configured.
5. **ProviderConfig (provider-kubernetes)** — references the GKE cluster's
   kubeconfig connection secret so downstream compositions can create K8s
   resources on the cluster.
6. **ProviderConfig (provider-helm)** — same kubeconfig, for Helm releases.

### Composition function: `function-modelplane-gke`

```python
import grpc

from crossplane.function import logging, resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from crossplane.function.proto.v1 import run_function_pb2_grpc as grpcv1

from .model.io.upbound.gcp.compute.network import v1beta2 as networkv1beta2
from .model.io.upbound.gcp.compute.subnetwork import v1beta2 as subnetv1beta2
from .model.io.upbound.gcp.container.cluster import v1beta2 as clusterv1beta2
from .model.io.upbound.gcp.container.nodepool import v1beta2 as nodepoolv1beta2
from .model.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1
from .model.ai.modelplane.infrastructure.gkecluster import v1alpha1


class FunctionRunner(grpcv1.FunctionRunnerService):
    def __init__(self):
        self.log = logging.get_logger()

    async def RunFunction(
        self, req: fnv1.RunFunctionRequest, _: grpc.aio.ServicerContext
    ) -> fnv1.RunFunctionResponse:
        log = self.log.bind(tag=req.meta.tag)
        rsp = response.to(req)

        xr = v1alpha1.GKECluster(**req.observed.composite.resource)
        name = xr.metadata.name  # type: ignore
        spec = xr.spec

        # --- VPC Network ---
        resource.update(
            rsp.desired.resources["network"],
            networkv1beta2.Network(
                spec=networkv1beta2.Spec(
                    forProvider=networkv1beta2.ForProvider(
                        project=spec.project,
                        autoCreateSubnetworks=False,
                    ),
                ),
            ),
        )

        # --- Subnetwork ---
        networking = spec.networking or v1alpha1.Networking()
        resource.update(
            rsp.desired.resources["subnet"],
            subnetv1beta2.Subnetwork(
                spec=subnetv1beta2.Spec(
                    forProvider=subnetv1beta2.ForProvider(
                        project=spec.project,
                        region=spec.region,
                        networkSelector=subnetv1beta2.NetworkSelector(
                            matchControllerRef=True,
                        ),
                        ipCidrRange=networking.nodeCidr or "10.0.0.0/24",
                        secondaryIpRange=[
                            subnetv1beta2.SecondaryIpRangeItem(
                                rangeName="pods",
                                ipCidrRange=networking.podCidr or "10.1.0.0/16",
                            ),
                            subnetv1beta2.SecondaryIpRangeItem(
                                rangeName="services",
                                ipCidrRange=networking.serviceCidr or "10.2.0.0/16",
                            ),
                        ],
                    ),
                ),
            ),
        )

        # --- GKE Cluster ---
        resource.update(
            rsp.desired.resources["cluster"],
            clusterv1beta2.Cluster(
                spec=clusterv1beta2.Spec(
                    forProvider=clusterv1beta2.ForProvider(
                        project=spec.project,
                        location=spec.region,
                        deletionProtection=False,
                        removeDefaultNodePool=True,
                        initialNodeCount=1,
                        minMasterVersion=spec.kubernetesVersion or "1.31",
                        networkSelector=clusterv1beta2.NetworkSelector(
                            matchControllerRef=True,
                        ),
                        subnetworkSelector=clusterv1beta2.SubnetworkSelector(
                            matchControllerRef=True,
                        ),
                        ipAllocationPolicy=clusterv1beta2.IpAllocationPolicy(
                            clusterSecondaryRangeName="pods",
                            servicesSecondaryRangeName="services",
                        ),
                        releaseChannel=clusterv1beta2.ReleaseChannel(
                            channel="REGULAR",
                        ),
                        workloadIdentityConfig=clusterv1beta2.WorkloadIdentityConfig(
                            workloadPool=f"{spec.project}.svc.id.goog",
                        ),
                    ),
                    writeConnectionSecretToRef=clusterv1beta2.WriteConnectionSecretToRef(
                        name=f"{name}-kubeconfig",
                        namespace="crossplane-system",
                    ),
                ),
            ),
        )

        # --- GPU Node Pool ---
        np = spec.nodePool
        resource.update(
            rsp.desired.resources["gpu-nodepool"],
            nodepoolv1beta2.NodePool(
                spec=nodepoolv1beta2.Spec(
                    forProvider=nodepoolv1beta2.ForProvider(
                        project=spec.project,
                        location=spec.region,
                        clusterSelector=nodepoolv1beta2.ClusterSelector(
                            matchControllerRef=True,
                        ),
                        initialNodeCount=np.nodeCount or 1,
                        autoscaling=nodepoolv1beta2.Autoscaling(
                            minNodeCount=np.minNodeCount or 0,
                            maxNodeCount=np.maxNodeCount or 8,
                        ),
                        nodeConfig=nodepoolv1beta2.NodeConfig(
                            machineType=np.machineType,
                            diskSizeGb=np.diskSizeGb or 200,
                            imageType="COS_CONTAINERD",
                            guestAccelerator=[
                                nodepoolv1beta2.GuestAcceleratorItem(
                                    type=np.acceleratorType,
                                    count=np.acceleratorCount or 1,
                                    gpuDriverInstallationConfig=nodepoolv1beta2.GpuDriverInstallationConfig(
                                        gpuDriverVersion="DEFAULT",
                                    ),
                                ),
                            ],
                            oauthScopes=[
                                "https://www.googleapis.com/auth/cloud-platform",
                            ],
                            taint=[
                                nodepoolv1beta2.TaintItem(
                                    key="nvidia.com/gpu",
                                    value="true",
                                    effect="NO_SCHEDULE",
                                ),
                            ],
                            labels={
                                "modelplane.ai/gpu": np.acceleratorType,
                            },
                        ),
                    ),
                ),
            ),
        )

        # --- ProviderConfigs ---
        # These use the GKE cluster's connection secret (kubeconfig) so that
        # downstream resources (KServeStack, InferenceEnvironment K8s Objects)
        # can target this cluster.
        pc_name = f"{name}-kubeconfig"

        resource.update(
            rsp.desired.resources["provider-config-kubernetes"],
            {
                "apiVersion": "kubernetes.crossplane.io/v1alpha1",
                "kind": "ProviderConfig",
                "metadata": {
                    "name": pc_name,
                },
                "spec": {
                    "credentials": {
                        "source": "Secret",
                        "secretRef": {
                            "name": f"{name}-kubeconfig",
                            "namespace": "crossplane-system",
                            "key": "kubeconfig",
                        },
                    },
                },
            },
        )

        resource.update(
            rsp.desired.resources["provider-config-helm"],
            {
                "apiVersion": "helm.crossplane.io/v1beta1",
                "kind": "ProviderConfig",
                "metadata": {
                    "name": pc_name,
                },
                "spec": {
                    "credentials": {
                        "source": "Secret",
                        "secretRef": {
                            "name": f"{name}-kubeconfig",
                            "namespace": "crossplane-system",
                            "key": "kubeconfig",
                        },
                    },
                },
            },
        )

        # --- XR Status ---
        resource.update(rsp.desired.composite, {
            "status": {
                "providerConfigRef": {"name": pc_name},
            },
        })

        response.normal(rsp, f"Composed GKE cluster in {spec.region}")
        return rsp
```

Note: The ProviderConfigs are composed as raw dicts rather than Pydantic models
because ProviderConfig types aren't managed resources — they don't have
`forProvider` / `atProvider` sections and Pydantic models aren't generated for
them. This is a common pattern in Crossplane composition functions.

---

## KServeStack XR

### XRD

```yaml
apiVersion: apiextensions.crossplane.io/v2
kind: CompositeResourceDefinition
metadata:
  name: kservestacks.infrastructure.modelplane.ai
spec:
  scope: Cluster
  group: infrastructure.modelplane.ai
  names:
    kind: KServeStack
    plural: kservestacks
  versions:
  - name: v1alpha1
    served: true
    referenceable: true
    schema:
      openAPIV3Schema:
        type: object
        properties:
          spec:
            type: object
            required: [providerConfigRef]
            properties:
              providerConfigRef:
                type: object
                required: [name]
                properties:
                  name:
                    type: string
                    description: >-
                      ProviderConfig for provider-kubernetes and provider-helm
                      targeting the cluster where KServe should be installed.
              version:
                type: string
                default: "v0.16.0"
                description: KServe LLMInferenceService version.
              certManager:
                type: object
                properties:
                  install:
                    type: boolean
                    default: true
                    description: >-
                      Install cert-manager. Set to false if already present.
                  version:
                    type: string
                    default: "v1.17.1"
              envoyGateway:
                type: object
                properties:
                  version:
                    type: string
                    default: "v1.3.0"
                  enableBackendApi:
                    type: boolean
                    default: true
                    description: >-
                      Enable the Backend API for cross-cluster routing. Required
                      for unified endpoint routing in ModelDeployment.
              gateway:
                type: object
                properties:
                  className:
                    type: string
                    default: envoy
                  listeners:
                    type: array
                    items:
                      type: object
                      properties:
                        port:
                          type: integer
                        protocol:
                          type: string
          status:
            type: object
            properties:
              conditions:
                type: array
                items:
                  type: object
                  properties:
                    type:
                      type: string
                    status:
                      type: string
                    reason:
                      type: string
                    message:
                      type: string
              gateway:
                type: object
                properties:
                  address:
                    type: string
                    description: >-
                      The gateway's external address, once assigned.
```

### What it composes

Seven Helm releases and two Kubernetes objects, all targeting the remote cluster
via `providerConfigRef`:

**Helm releases (via provider-helm):**

1. **cert-manager** — TLS certificate management. Conditional on
   `spec.certManager.install`.
2. **Gateway API CRDs** — the base gateway.networking.k8s.io CRDs.
3. **Envoy Gateway** — Gateway API implementation.
4. **Envoy AI Gateway** — AI-specific traffic management (optional for v0.1,
   but installed to support unified endpoint routing).
5. **LeaderWorkerSet** — multi-node pod coordination for large models.
6. **kserve-llmisvc-crd** — KServe LLMInferenceService CRDs.
7. **kserve-llmisvc-resources** — KServe LLMInferenceService controller.

**Kubernetes objects (via provider-kubernetes):**

8. **GatewayClass** — registers the Envoy gateway class.
9. **Gateway** — the cluster's ingress gateway for inference traffic.

The function installs these in dependency order. Helm releases that depend on
CRDs from earlier releases use `spec.forProvider.skipCreateNamespace: false` and
appropriate `dependsOn` annotations or Crossplane readiness checks.

### Composition function: `function-modelplane-kserve`

```python
import grpc

from crossplane.function import logging, resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from crossplane.function.proto.v1 import run_function_pb2_grpc as grpcv1

from .model.io.crossplane.helm.release import v1beta1 as helmv1beta1
from .model.ai.modelplane.infrastructure.kservestack import v1alpha1


def _helm_release(
    name: str,
    chart: str,
    repo: str,
    version: str,
    namespace: str,
    provider_config: str,
    values: dict | None = None,
    depends_on: list[str] | None = None,
) -> helmv1beta1.Release:
    """Build a Helm Release with common settings."""
    release = helmv1beta1.Release(
        spec=helmv1beta1.Spec(
            providerConfigRef=helmv1beta1.ProviderConfigRef(
                name=provider_config,
            ),
            forProvider=helmv1beta1.ForProvider(
                chart=helmv1beta1.Chart(
                    name=chart,
                    repository=repo,
                    version=version,
                ),
                namespace=namespace,
                wait=True,
                waitTimeout=600,
            ),
        ),
    )
    if values:
        release.spec.forProvider.values = values  # type: ignore
    return release


class FunctionRunner(grpcv1.FunctionRunnerService):
    def __init__(self):
        self.log = logging.get_logger()

    async def RunFunction(
        self, req: fnv1.RunFunctionRequest, _: grpc.aio.ServicerContext
    ) -> fnv1.RunFunctionResponse:
        log = self.log.bind(tag=req.meta.tag)
        rsp = response.to(req)

        xr = v1alpha1.KServeStack(**req.observed.composite.resource)
        pc = xr.spec.providerConfigRef.name
        kserve_version = xr.spec.version or "v0.16.0"

        # --- cert-manager ---
        cert_manager = xr.spec.certManager or v1alpha1.CertManager()
        if cert_manager.install is not False:
            resource.update(
                rsp.desired.resources["cert-manager"],
                _helm_release(
                    name="cert-manager",
                    chart="cert-manager",
                    repo="https://charts.jetstack.io",
                    version=cert_manager.version or "v1.17.1",
                    namespace="cert-manager",
                    provider_config=pc,
                    values={"crds": {"enabled": True}},
                ),
            )

        # --- Gateway API CRDs ---
        resource.update(
            rsp.desired.resources["gateway-api"],
            _helm_release(
                name="gateway-api",
                chart="gateway-api",
                repo="https://gateway-api-helm.sigs.k8s.io",
                version="v1.2.1",
                namespace="gateway-system",
                provider_config=pc,
            ),
        )

        # --- Envoy Gateway ---
        eg = xr.spec.envoyGateway or v1alpha1.EnvoyGateway()
        eg_values = {}
        if eg.enableBackendApi is not False:
            eg_values = {
                "config": {
                    "envoyGateway": {
                        "extensionApis": {"enableBackend": True},
                    },
                },
            }
        resource.update(
            rsp.desired.resources["envoy-gateway"],
            _helm_release(
                name="envoy-gateway",
                chart="gateway-helm",
                repo="oci://docker.io/envoyproxy",
                version=eg.version or "v1.3.0",
                namespace="envoy-gateway-system",
                provider_config=pc,
                values=eg_values,
            ),
        )

        # --- LeaderWorkerSet ---
        resource.update(
            rsp.desired.resources["leader-worker-set"],
            _helm_release(
                name="lws",
                chart="lws",
                repo="https://kubernetes-sigs.github.io/lws",
                version="0.6.0",
                namespace="lws-system",
                provider_config=pc,
            ),
        )

        # --- KServe LLMInferenceService CRDs ---
        resource.update(
            rsp.desired.resources["kserve-crds"],
            _helm_release(
                name="kserve-llmisvc-crd",
                chart="kserve-llmisvc-crd",
                repo="oci://ghcr.io/kserve/charts",
                version=kserve_version,
                namespace="kserve",
                provider_config=pc,
            ),
        )

        # --- KServe LLMInferenceService controller ---
        resource.update(
            rsp.desired.resources["kserve-controller"],
            _helm_release(
                name="kserve-llmisvc-resources",
                chart="kserve-llmisvc-resources",
                repo="oci://ghcr.io/kserve/charts",
                version=kserve_version,
                namespace="kserve",
                provider_config=pc,
            ),
        )

        # --- GatewayClass (via provider-kubernetes Object) ---
        gw = xr.spec.gateway or v1alpha1.Gateway()
        gw_class_name = gw.className or "envoy"

        resource.update(
            rsp.desired.resources["gateway-class"],
            {
                "apiVersion": "kubernetes.crossplane.io/v1alpha2",
                "kind": "Object",
                "spec": {
                    "providerConfigRef": {"name": pc},
                    "forProvider": {
                        "manifest": {
                            "apiVersion": "gateway.networking.k8s.io/v1",
                            "kind": "GatewayClass",
                            "metadata": {
                                "name": gw_class_name,
                            },
                            "spec": {
                                "controllerName": "gateway.envoyproxy.io/gatewayclass-controller",
                            },
                        },
                    },
                },
            },
        )

        # --- Gateway ---
        resource.update(
            rsp.desired.resources["gateway"],
            {
                "apiVersion": "kubernetes.crossplane.io/v1alpha2",
                "kind": "Object",
                "spec": {
                    "providerConfigRef": {"name": pc},
                    "forProvider": {
                        "manifest": {
                            "apiVersion": "gateway.networking.k8s.io/v1",
                            "kind": "Gateway",
                            "metadata": {
                                "name": "modelplane",
                                "namespace": "envoy-gateway-system",
                            },
                            "spec": {
                                "gatewayClassName": gw_class_name,
                                "listeners": [
                                    {
                                        "name": "http",
                                        "protocol": "HTTP",
                                        "port": 80,
                                    },
                                ],
                            },
                        },
                    },
                },
            },
        )

        response.normal(rsp, f"Composed KServe {kserve_version} stack")
        return rsp
```

GatewayClass and Gateway are composed as `provider-kubernetes` Object resources
rather than direct Kubernetes resources because they target a remote cluster.
The Object wrapper carries the `providerConfigRef` that tells
provider-kubernetes which cluster to create the resource on. Helm releases use
the same ProviderConfig via provider-helm's built-in `providerConfigRef`.

---

## InferenceEnvironment integration

### XRD addition

The InferenceEnvironment XRD gets an optional `spec.cluster.provision` section
for cloud-specific cluster provisioning. The existing `providerConfigRef` field
remains — it's used when `provision` is absent (the existing-cluster path).

```yaml
# Addition to the InferenceEnvironment XRD's spec.cluster
cluster:
  type: object
  properties:
    providerConfigRef:
      type: object
      properties:
        name:
          type: string
    provision:
      type: object
      description: >-
        Provision a new cluster. Mutually exclusive with providerConfigRef.
        The Composition variant determines which cloud provider is used.
      properties:
        project:
          type: string
        region:
          type: string
        kubernetesVersion:
          type: string
        nodePool:
          type: object
          properties:
            machineType:
              type: string
            acceleratorType:
              type: string
            acceleratorCount:
              type: integer
            nodeCount:
              type: integer
            minNodeCount:
              type: integer
            maxNodeCount:
              type: integer
```

### GKE Composition variant

The GKE-specific Composition for InferenceEnvironment uses a function that
composes `GKECluster` and `KServeStack` XRs, then handles environment-level
configuration (LLMInferenceServiceConfig, LocalModelNodeGroup, RBAC). The
`composition.yaml` selects this variant by label:

```yaml
apiVersion: apiextensions.crossplane.io/v1
kind: Composition
metadata:
  name: inferenceenvironment-gke
  labels:
    modelplane.ai/provider: gke
spec:
  compositeTypeRef:
    apiVersion: modelplane.ai/v1alpha1
    kind: InferenceEnvironment
  mode: Pipeline
  pipeline:
  - step: compose-gke-and-kserve
    functionRef:
      name: function-modelplane-env-gke
```

The function:

```python
async def RunFunction(
    self, req: fnv1.RunFunctionRequest, _: grpc.aio.ServicerContext
) -> fnv1.RunFunctionResponse:
    rsp = response.to(req)

    xr = v1alpha1.InferenceEnvironment(**req.observed.composite.resource)
    name = xr.metadata.name  # type: ignore
    provision = xr.spec.cluster.provision

    if provision is None:
        response.fatal(rsp, "GKE composition requires spec.cluster.provision")
        return rsp

    # --- Compose GKECluster XR ---
    resource.update(
        rsp.desired.resources["gke-cluster"],
        {
            "apiVersion": "infrastructure.modelplane.ai/v1alpha1",
            "kind": "GKECluster",
            "metadata": {"name": name},
            "spec": {
                "project": provision.project,
                "region": provision.region,
                "kubernetesVersion": provision.kubernetesVersion or "1.31",
                "nodePool": {
                    "machineType": provision.nodePool.machineType,
                    "acceleratorType": provision.nodePool.acceleratorType,
                    "acceleratorCount": provision.nodePool.acceleratorCount or 1,
                    "nodeCount": provision.nodePool.nodeCount or 1,
                    "minNodeCount": provision.nodePool.minNodeCount or 0,
                    "maxNodeCount": provision.nodePool.maxNodeCount or 8,
                },
            },
        },
    )

    # Read the GKECluster's status to get the ProviderConfig name.
    # On the first reconciliation this won't exist yet — the function
    # returns early and Crossplane re-reconciles once the GKECluster
    # reports its status.
    gke_status = None
    if "gke-cluster" in req.observed.resources:
        gke_observed = req.observed.resources["gke-cluster"].resource
        gke_status = gke_observed.get("status", {})

    pc_name = None
    if gke_status:
        pc_ref = gke_status.get("providerConfigRef", {})
        pc_name = pc_ref.get("name")

    if not pc_name:
        response.normal(rsp, "Waiting for GKE cluster to report ProviderConfig")
        return rsp

    # --- Compose KServeStack XR ---
    kserve_spec = xr.spec.kserve or v1alpha1.KServe()
    resource.update(
        rsp.desired.resources["kserve-stack"],
        {
            "apiVersion": "infrastructure.modelplane.ai/v1alpha1",
            "kind": "KServeStack",
            "spec": {
                "providerConfigRef": {"name": pc_name},
                "version": kserve_spec.version or "v0.16.0",
                "envoyGateway": {
                    "enableBackendApi": True,
                },
            },
        },
    )

    # --- Environment-level K8s Objects ---
    # LLMInferenceServiceConfig, LocalModelNodeGroup, RBAC — same as
    # the existing function-modelplane-env, targeting the remote cluster
    # via pc_name. Omitted here for brevity; see the PRD's
    # function-modelplane-env specification.

    response.normal(rsp, f"Composed GKE InferenceEnvironment in {provision.region}")
    return rsp
```

The function composes `GKECluster` and `KServeStack` as XRs — Crossplane
reconciles them through their own composition pipelines. The
`function-modelplane-env-gke` function doesn't know about GCE machine types or
Helm charts; it delegates to the specialist XRs.

---

## End-to-end example

A platform team provisions a GKE-backed InferenceEnvironment:

```yaml
apiVersion: modelplane.ai/v1alpha1
kind: InferenceEnvironment
metadata:
  name: gpu-cluster-us-central
  labels:
    modelplane.ai/tier: production
    modelplane.ai/region: us-central
    modelplane.ai/gpu: A100
spec:
  crossplane:
    compositionRef:
      name: inferenceenvironment-gke

  cluster:
    provision:
      project: acme-ml-platform
      region: us-central1
      nodePool:
        machineType: a2-highgpu-8g
        acceleratorType: nvidia-tesla-a100
        acceleratorCount: 8
        nodeCount: 2
        maxNodeCount: 8

  backend: KServe
  kserve:
    version: v0.16.0

  engine:
    image: vllm/vllm-openai:v0.16.0
    prefixCaching: true
    gpuMemoryUtilization: 0.9

  capacity:
    gpuTypes:
    - type: nvidia.com/gpu
      model: A100
      vram: 80Gi
      available: 16
    nodeGroups:
    - name: gpu-workers
      nodeSelector:
        cloud.google.com/gke-accelerator: nvidia-tesla-a100

  modelCache:
    policy: Enabled
    storageClass: local-nvme
    storageCapacity: 500Gi
    nodeSelector:
      cloud.google.com/gke-accelerator: nvidia-tesla-a100
```

Under the hood, Crossplane creates:

```
InferenceEnvironment: gpu-cluster-us-central
├─ GKECluster: gpu-cluster-us-central
│  ├─ compute.gcp.upbound.io Network
│  ├─ compute.gcp.upbound.io Subnetwork
│  ├─ container.gcp.upbound.io Cluster
│  ├─ container.gcp.upbound.io NodePool (8x A100)
│  ├─ ProviderConfig (provider-kubernetes)
│  └─ ProviderConfig (provider-helm)
├─ KServeStack: gpu-cluster-us-central-kserve
│  ├─ Helm Release: cert-manager
│  ├─ Helm Release: gateway-api
│  ├─ Helm Release: envoy-gateway
│  ├─ Helm Release: leader-worker-set
│  ├─ Helm Release: kserve-llmisvc-crd
│  ├─ Helm Release: kserve-llmisvc-resources
│  ├─ Object: GatewayClass
│  └─ Object: Gateway
├─ Object: LLMInferenceServiceConfig
├─ Object: LocalModelNodeGroup
└─ ClusterRole: RBAC grants
```

An ML team then creates a ModelDeployment targeting this environment — exactly
the same experience whether the cluster was pre-existing or provisioned by
Modelplane.

---

## Alternatives considered

### Single XR: GKEKServeCluster

One XR that provisions GKE and installs KServe in a single composition. Fewer
resources to manage, no cross-XR ProviderConfig wiring, simpler to reason
about for the demo.

I decided against it for the reasons in the opening section — lifecycle
coupling, no reuse, and the v0.2 backend problem. The stronger argument is that
`GKECluster` is genuinely useful on its own. Platform teams using Crossplane for
GKE today would benefit from a standardized `GKECluster` XR whether or not
they're running inference workloads.

### KServe installation stays in function-modelplane-env

The PRD's current design has `function-modelplane-env` installing KServe directly
via Helm releases, without a `KServeStack` XR. The argument for this is
simplicity — one fewer XR, one fewer function. The env function already knows
about the backend (it reads `spec.backend: KServe`), so it can just compose the
Helm releases itself.

I'd consider this a reasonable alternative for v0.1. The KServeStack extraction
becomes compelling when a second backend arrives and you want clean separation
between "install backend X" and "configure the environment." If v0.1 needs to
ship fast and KubeAI is definitely v0.2, keeping KServe installation in
`function-modelplane-env` is a pragmatic choice. The extraction can happen later
without API changes — it's an internal refactor of what the env function
composes.

### GKECluster lives outside InferenceEnvironment entirely

The PRD's original design has GKE provisioning completely separate — the
platform team creates a GKE cluster through whatever means they prefer, then
creates an InferenceEnvironment referencing its ProviderConfig. The example
compositions are just standalone YAML files showing both resources, not a
composition that creates them together.

This is valid and I'd want to support it alongside the integrated path. Some
platform teams have their own cluster provisioning pipeline (Terraform, Cluster
API, a custom Crossplane composition) and don't want Modelplane touching it. The
`spec.cluster.providerConfigRef` path in the InferenceEnvironment XRD serves
these teams. The `spec.cluster.provision` path serves teams who want the
integrated experience. The two paths coexist cleanly because they're separate
fields, and the Composition variant determines which path runs.

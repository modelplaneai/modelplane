import json
from pathlib import Path

from crossplane.function import logging, resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1

from .model.io.crossplane.helm.release import v1beta1 as helmv1beta1
from .model.ai.modelplane.infrastructure.kservestack import v1alpha1

_HERE = Path(__file__).parent

_INFERENCE_EXTENSION_CRDS = json.loads(
    (_HERE / "inference_extension_crds.json").read_text()
)

_STORAGE_INITIALIZER_CONFIG = json.dumps({
    "image": "kserve/storage-initializer:latest",
    "memoryRequest": "100Mi",
    "memoryLimit": "4Gi",
    "cpuRequest": "100m",
    "cpuLimit": "1",
    "caBundleConfigMapName": "",
    "caBundleVolumeMountPath": "/etc/ssl/custom-certs",
    "enableModelcar": True,
    "cpuModelcar": "10m",
    "memoryModelcar": "15Mi",
    "uidModelcar": 1010,
})

_KUSTOMIZE_STORAGE_PATCH = json.dumps({
    "patches": [{
        "patch": json.dumps({
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "inferenceservice-config"},
            "data": {"storageInitializer": _STORAGE_INITIALIZER_CONFIG},
        }),
        "target": {
            "kind": "ConfigMap",
            "name": "inferenceservice-config",
        },
    }],
})


def _is_ready(req: fnv1.RunFunctionRequest, name: str) -> bool:
    observed = req.observed.resources.get(name)
    if observed is None:
        return False
    c = resource.get_condition(observed.resource, "Ready")
    return c.status == "True"


def _helm_release(
    chart: str,
    repo: str,
    version: str,
    namespace: str,
    provider_config: str,
    values: dict | None = None,
) -> helmv1beta1.Release:
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
            ),
        ),
    )
    if values:
        release.spec.forProvider.values = values
    return release


def _k8s_object(provider_config: str, manifest: dict) -> dict:
    return {
        "apiVersion": "kubernetes.crossplane.io/v1alpha2",
        "kind": "Object",
        "spec": {
            "providerConfigRef": {"name": provider_config},
            "forProvider": {"manifest": manifest},
        },
    }


def compose(req: fnv1.RunFunctionRequest, rsp: fnv1.RunFunctionResponse):
    xr = v1alpha1.KServeStack(**resource.struct_to_dict(req.observed.composite.resource))
    pc = xr.spec.providerConfigRef.name
    v = xr.spec.versions or v1alpha1.Versions()

    resource.update(
        rsp.desired.resources["cert-manager"],
        _helm_release(
            chart="cert-manager",
            repo="https://charts.jetstack.io",
            version=v.certManager or "v1.17.1",
            namespace="cert-manager",
            provider_config=pc,
            values={"crds": {"enabled": True, "keep": False}},
        ),
    )

    resource.update(
        rsp.desired.resources["envoy-gateway"],
        _helm_release(
            chart="gateway-helm",
            repo="oci://docker.io/envoyproxy",
            version=v.envoyGateway or "v1.3.0",
            namespace="envoy-gateway-system",
            provider_config=pc,
            values={
                "config": {
                    "envoyGateway": {
                        "extensionApis": {"enableBackend": True},
                    },
                },
            },
        ),
    )

    resource.update(
        rsp.desired.resources["leader-worker-set"],
        _helm_release(
            chart="lws",
            repo="oci://registry.k8s.io/lws/charts",
            version=v.leaderWorkerSet or "v0.7.0",
            namespace="lws-system",
            provider_config=pc,
        ),
    )

    resource.update(
        rsp.desired.resources["kserve-crds"],
        _helm_release(
            chart="kserve-llmisvc-crd",
            repo="oci://ghcr.io/kserve/charts",
            version=v.kserve or "v0.16.0",
            namespace="kserve",
            provider_config=pc,
        ),
    )

    # TODO(negz): File an upstream issue with kserve/kserve to expose
    # storageInitializer config fields (especially memoryLimit) as Helm values.
    #
    # The kserve-llmisvc-resources chart v0.16.0 has a static ConfigMap template
    # (inferenceservice-config) that hardcodes storageInitializer.memoryLimit to
    # 1Gi. The entire storageInitializer JSON blob is inlined in the template
    # with no {{ .Values }} references, so there's no way to override it via
    # Helm values. The 1Gi default causes OOM when the storage initializer
    # downloads models larger than ~1GB (even the 3GB Qwen2.5-1.5B triggers it).
    #
    # We work around this using provider-helm's patchesFrom, which applies a
    # Kustomize strategic merge patch post-render. This makes Helm's desired
    # state include 4Gi, avoiding a fight between Helm and any manual patch.
    # The fix upstream would be to template the storageInitializer fields behind
    # .Values so consumers can set memoryLimit (and other fields) normally.
    patch_cm_name = f"{xr.metadata.name}-storage-patch"

    resource.update(
        rsp.desired.resources["kserve-storage-patch"],
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": patch_cm_name,
                "namespace": "crossplane-system",
            },
            "data": {
                "patches": _KUSTOMIZE_STORAGE_PATCH,
            },
        },
    )

    kserve_release = _helm_release(
        chart="kserve-llmisvc-resources",
        repo="oci://ghcr.io/kserve/charts",
        version=v.kserve or "v0.16.0",
        namespace="kserve",
        provider_config=pc,
    )
    kserve_release.spec.forProvider.patchesFrom = [
        helmv1beta1.PatchesFromItem(
            configMapKeyRef=helmv1beta1.ConfigMapKeyRef(
                name=patch_cm_name,
                namespace="crossplane-system",
                key="patches",
            ),
        ),
    ]
    resource.update(
        rsp.desired.resources["kserve-controller"],
        kserve_release,
    )

    # KServe v0.16 needs Gateway API Inference Extension CRDs (InferencePool,
    # InferenceModel) but doesn't install them. We install each CRD as a
    # provider-kubernetes Object.
    for i, crd in enumerate(_INFERENCE_EXTENSION_CRDS):
        crd_name = crd["metadata"]["name"]
        short = crd_name.split(".")[0]
        resource.update(
            rsp.desired.resources[f"inference-ext-crd-{short}"],
            _k8s_object(pc, crd),
        )

    gw = xr.spec.gateway or v1alpha1.Gateway()
    gw_class_name = gw.className or "envoy"

    if gw.listeners:
        listeners = [
            {"name": l.name, "protocol": l.protocol, "port": l.port}
            for l in gw.listeners
        ]
    else:
        listeners = [{"name": "http", "protocol": "HTTP", "port": 80}]

    resource.update(
        rsp.desired.resources["gateway-class"],
        _k8s_object(pc, {
            "apiVersion": "gateway.networking.k8s.io/v1",
            "kind": "GatewayClass",
            "metadata": {"name": gw_class_name},
            "spec": {
                "controllerName": "gateway.envoyproxy.io/gatewayclass-controller",
            },
        }),
    )

    gw_listeners = [
        {**l, "allowedRoutes": {"namespaces": {"from": "All"}}}
        for l in listeners
    ]

    resource.update(
        rsp.desired.resources["gateway"],
        _k8s_object(pc, {
            "apiVersion": "gateway.networking.k8s.io/v1",
            "kind": "Gateway",
            "metadata": {
                "name": "kserve-ingress-gateway",
                "namespace": "kserve",
            },
            "spec": {
                "gatewayClassName": gw_class_name,
                "listeners": gw_listeners,
            },
        }),
    )

    all_resources = [
        "cert-manager", "envoy-gateway",
        "leader-worker-set",
        "kserve-crds", "kserve-controller",
        "inference-ext-crd-inferencemodels",
        "inference-ext-crd-inferencepools",
        "gateway-class", "gateway",
    ]

    rsp.desired.resources["kserve-storage-patch"].ready = fnv1.READY_TRUE

    all_ready = True
    not_ready = []
    for r in all_resources:
        if _is_ready(req, r):
            rsp.desired.resources[r].ready = fnv1.READY_TRUE
        else:
            all_ready = False
            not_ready.append(r)

    if all_ready:
        rsp.conditions.append(fnv1.Condition(
            type="Ready",
            status=fnv1.STATUS_CONDITION_TRUE,
            reason="Available",
            target=fnv1.TARGET_COMPOSITE_AND_CLAIM,
        ))
    else:
        rsp.conditions.append(fnv1.Condition(
            type="Ready",
            status=fnv1.STATUS_CONDITION_FALSE,
            reason="Creating",
            message=f"Waiting for: {', '.join(not_ready)}",
            target=fnv1.TARGET_COMPOSITE_AND_CLAIM,
        ))

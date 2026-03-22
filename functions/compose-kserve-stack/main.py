import json
from pathlib import Path

from crossplane.function import resource
from crossplane.function.proto.v1 import run_function_pb2 as fnv1

from .model.io.crossplane.m.helm.release import v1beta1 as helmv1beta1
from .model.io.crossplane.m.helm.providerconfig import v1beta1 as helmpcv1beta1
from .model.io.crossplane.m.kubernetes.providerconfig import v1alpha1 as k8spcv1alpha1
from .model.io.crossplane.m.kubernetes.object import v1alpha1 as k8sobjv1alpha1
from .model.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1
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


def _has_condition(req: fnv1.RunFunctionRequest, name: str, cond: str) -> bool:
    """Check if an observed composed resource has the given condition True."""
    observed = req.observed.resources.get(name)
    if observed is None:
        return False
    return resource.get_condition(observed.resource, cond).status == "True"


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
                kind="ProviderConfig",
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


def _k8s_object(
    provider_config: str,
    manifest: dict,
    labels: dict | None = None,
    management_policies: list | None = None,
) -> k8sobjv1alpha1.Object:
    obj = k8sobjv1alpha1.Object(
        spec=k8sobjv1alpha1.Spec(
            providerConfigRef=k8sobjv1alpha1.ProviderConfigRef(
                kind="ProviderConfig",
                name=provider_config,
            ),
            forProvider=k8sobjv1alpha1.ForProvider(
                manifest=manifest,
            ),
        ),
    )
    if labels:
        obj.metadata = metav1.ObjectMeta(labels=labels)
    if management_policies:
        obj.spec.managementPolicies = management_policies
    return obj


def compose(req: fnv1.RunFunctionRequest, rsp: fnv1.RunFunctionResponse):
    xr = v1alpha1.KServeStack(**resource.struct_to_dict(req.observed.composite.resource))
    name = xr.metadata.name
    ns = xr.metadata.namespace
    v = xr.spec.versions or v1alpha1.Versions()

    secrets = xr.spec.secrets or []

    kubeconfig_secret = next(
        (s for s in secrets if s.type == "Kubeconfig"), None
    )

    if not kubeconfig_secret:
        rsp.conditions.append(fnv1.Condition(
            type="Ready",
            status=fnv1.STATUS_CONDITION_FALSE,
            reason="InvalidSpec",
            message="spec.secrets must include a Kubeconfig entry",
            target=fnv1.TARGET_COMPOSITE_AND_CLAIM,
        ))
        return

    # Build ProviderConfig specs from the secrets array. The kubeconfig
    # provides the cluster endpoint and CA cert. If a cloud-specific
    # credential secret is present, it's layered on as an identity block
    # so the provider authenticates via the cloud's IAM instead of
    # relying on whatever auth is baked into the kubeconfig.
    k8s_pc_spec = k8spcv1alpha1.Spec(
        credentials=k8spcv1alpha1.Credentials(
            source="Secret",
            secretRef=k8spcv1alpha1.SecretRef(
                name=kubeconfig_secret.name,
                namespace=ns,
                key=kubeconfig_secret.key,
            ),
        ),
    )
    helm_pc_spec = helmpcv1beta1.Spec(
        credentials=helmpcv1beta1.Credentials(
            source="Secret",
            secretRef=helmpcv1beta1.SecretRef(
                name=kubeconfig_secret.name,
                namespace=ns,
                key=kubeconfig_secret.key,
            ),
        ),
    )

    gcp_secret = next(
        (s for s in secrets if s.type == "GCPServiceAccountKey"), None
    )
    if gcp_secret:
        k8s_pc_spec.identity = k8spcv1alpha1.Identity(
            type="GoogleApplicationCredentials",
            source="Secret",
            secretRef=k8spcv1alpha1.SecretRef(
                name=gcp_secret.name,
                namespace=ns,
                key=gcp_secret.key,
            ),
        )
        helm_pc_spec.identity = helmpcv1beta1.Identity(
            type="GoogleApplicationCredentials",
            source="Secret",
            secretRef=helmpcv1beta1.SecretRef(
                name=gcp_secret.name,
                namespace=ns,
                key=gcp_secret.key,
            ),
        )

    pc_name = f"{name}-cluster"

    resource.update(
        rsp.desired.resources["provider-config-kubernetes"],
        k8spcv1alpha1.ProviderConfig(
            metadata=metav1.ObjectMeta(name=pc_name),
            spec=k8s_pc_spec,
        ),
    )

    resource.update(
        rsp.desired.resources["provider-config-helm"],
        helmpcv1beta1.ProviderConfig(
            metadata=metav1.ObjectMeta(name=pc_name),
            spec=helm_pc_spec,
        ),
    )

    # Protect the Helm ProviderConfig from deletion until all Helm Releases
    # that reference it are gone. Without this, deleting the KServeStack
    # deletes the ProviderConfig and Releases simultaneously — the Releases
    # can't uninstall their charts because the ProviderConfig is gone.
    resource.update(rsp.desired.resources["usage-helm-pc"], {
        "apiVersion": "protection.crossplane.io/v1beta1",
        "kind": "Usage",
        "spec": {
            "of": {
                "apiVersion": "helm.m.crossplane.io/v1beta1",
                "kind": "ProviderConfig",
                "resourceRef": {"name": pc_name},
            },
            "by": {
                "apiVersion": "helm.m.crossplane.io/v1beta1",
                "kind": "Release",
                "resourceSelector": {"matchControllerRef": True},
            },
            "replayDeletion": True,
        },
    })
    rsp.desired.resources["usage-helm-pc"].ready = fnv1.READY_TRUE

    # Same for the Kubernetes ProviderConfig — protect it until all Objects
    # that reference it are gone.
    resource.update(rsp.desired.resources["usage-k8s-pc"], {
        "apiVersion": "protection.crossplane.io/v1beta1",
        "kind": "Usage",
        "spec": {
            "of": {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "ProviderConfig",
                "resourceRef": {"name": pc_name},
            },
            "by": {
                "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                "kind": "Object",
                "resourceSelector": {"matchControllerRef": True},
            },
            "replayDeletion": True,
        },
    })
    rsp.desired.resources["usage-k8s-pc"].ready = fnv1.READY_TRUE

    # Gate resources that target the remote cluster on the ProviderConfigs
    # being observed — i.e. persisted by Crossplane from a previous reconcile.
    # This avoids transient "ProviderConfig not found" errors on first creation.
    pc_observed = (
        "provider-config-helm" in req.observed.resources
        and "provider-config-kubernetes" in req.observed.resources
    )

    if pc_observed:
        resource.update(
            rsp.desired.resources["cert-manager"],
            _helm_release(
                chart="cert-manager",
                repo="https://charts.jetstack.io",
                version=v.certManager or "v1.17.1",
                namespace="cert-manager",
                provider_config=pc_name,
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
                provider_config=pc_name,
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
                provider_config=pc_name,
            ),
        )

        for crd in _INFERENCE_EXTENSION_CRDS:
            crd_name = crd["metadata"]["name"]
            short = crd_name.split(".")[0]
            resource.update(
                rsp.desired.resources[f"inference-ext-crd-{short}"],
                _k8s_object(pc_name, crd),
            )

        gw = xr.spec.gateway or v1alpha1.Gateway()
        gw_class_name = gw.className or "envoy"

        if gw.listeners:
            listeners = [
                {"name": ln.name, "protocol": ln.protocol, "port": ln.port}
                for ln in gw.listeners
            ]
        else:
            listeners = [{"name": "http", "protocol": "HTTP", "port": 80}]

        # Don't delete the GatewayClass when KServeStack is deleted. The
        # remote GatewayClass has a gateway-exists-finalizer that blocks
        # deletion while any Gateway references it, causing the Object to
        # hang indefinitely. GatewayClass is cluster-level infrastructure
        # config — leaving it behind is harmless.
        resource.update(
            rsp.desired.resources["gateway-class"],
            _k8s_object(pc_name, {
                "apiVersion": "gateway.networking.k8s.io/v1",
                "kind": "GatewayClass",
                "metadata": {"name": gw_class_name},
                "spec": {
                    "controllerName": "gateway.envoyproxy.io/gatewayclass-controller",
                },
            }, management_policies=["Create", "Observe"]),
        )

        resource.update(
            rsp.desired.resources["gateway"],
            _k8s_object(pc_name, {
                "apiVersion": "gateway.networking.k8s.io/v1",
                "kind": "Gateway",
                "metadata": {
                    "name": "kserve-ingress-gateway",
                    "namespace": "kserve",
                },
                "spec": {
                    "gatewayClassName": gw_class_name,
                    "listeners": [
                        {**ln, "allowedRoutes": {"namespaces": {"from": "All"}}}
                        for ln in listeners
                    ],
                },
            }, labels={"modelplane.ai/resource": "gateway"}),
        )

    # Gate KServe CRDs and controller on cert-manager being ready. The kserve
    # chart creates Certificate and Issuer resources, and the kserve controller
    # registers a validating webhook that Helm calls during install. Both fail
    # if cert-manager isn't fully up.
    cert_manager_ready = _has_condition(req, "cert-manager", "Ready")

    if pc_observed and cert_manager_ready:
        resource.update(
            rsp.desired.resources["kserve-crds"],
            _helm_release(
                chart="kserve-llmisvc-crd",
                repo="oci://ghcr.io/kserve/charts",
                version=v.kserve or "v0.16.0",
                namespace="kserve",
                provider_config=pc_name,
            ),
        )

    patch_cm_name = f"{name}-storage-patch"

    resource.update(
        rsp.desired.resources["kserve-storage-patch"],
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": patch_cm_name,
                "namespace": ns,
            },
            "data": {
                "patches": _KUSTOMIZE_STORAGE_PATCH,
            },
        },
    )

    if pc_observed and cert_manager_ready:
        kserve_release = _helm_release(
            chart="kserve-llmisvc-resources",
            repo="oci://ghcr.io/kserve/charts",
            version=v.kserve or "v0.16.0",
            namespace="kserve",
            provider_config=pc_name,
        )
        kserve_release.spec.forProvider.patchesFrom = [
            helmv1beta1.PatchesFromItem(
                configMapKeyRef=helmv1beta1.ConfigMapKeyRef(
                    name=patch_cm_name,
                    namespace=ns,
                    key="patches",
                ),
            ),
        ]
        resource.update(rsp.desired.resources["kserve-controller"], kserve_release)

    always_ready = [
        "provider-config-kubernetes", "provider-config-helm",
        "kserve-storage-patch", "gateway-class",
    ]
    for r in always_ready:
        if r in rsp.desired.resources:
            rsp.desired.resources[r].ready = fnv1.READY_TRUE

    all_resources = [
        "cert-manager", "envoy-gateway",
        "leader-worker-set",
        "kserve-crds", "kserve-controller",
        "inference-ext-crd-inferencemodels",
        "inference-ext-crd-inferencepools",
        "gateway",
    ]

    all_ready = True
    not_ready = []
    for r in all_resources:
        if r not in rsp.desired.resources:
            all_ready = False
            not_ready.append(r)
        elif _has_condition(req, r, "Ready"):
            rsp.desired.resources[r].ready = fnv1.READY_TRUE
        else:
            all_ready = False
            not_ready.append(r)

    # Read the observed Gateway Object's status to extract the external IP
    # and write it to the XR's status.gateway.address.
    gateway_observed = req.observed.resources.get("gateway")
    if gateway_observed:
        gw = resource.struct_to_dict(gateway_observed.resource)
        addresses = (
            gw.get("status", {})
            .get("atProvider", {})
            .get("manifest", {})
            .get("status", {})
            .get("addresses", [])
        )
        if addresses:
            gateway_address = addresses[0].get("value")
            if gateway_address:
                resource.update(rsp.desired.composite, {
                    "status": {
                        "gateway": {"address": gateway_address},
                    },
                })

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

# Copyright 2026 The Modelplane Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Install the serving stack on a remote cluster.

The stack is a list of components fixed at build time: the function
joins the XR's cloud and stack through the stacks package (see
function/stacks/__init__.py and design/serving-stack-generation.md) and
renders each entry - a Chart as a provider-helm Release, a Manifests as
provider-kubernetes Objects - all targeting the remote cluster via
ProviderConfigs built from the XR's secrets. The reconcile path holds no
decisions of its own: every version, values block, and membership
decision was resolved where a human reviewed a diff.

Ordering derives from the data too, in both directions. A component's
depends_on edges become Usage resources holding a dependency until its
dependents are gone, and they gate installs: a component is first
created only once every dependency reports Ready, so bring-up proceeds
in dependency waves instead of relying on Helm retrying into absent
prerequisites. The one hand-rendered piece is the gateway pair - the
GatewayClass and Gateway read spec.gateway, which stays per-cluster
API - plus the Usages sequencing their teardown ahead of the Envoy
Gateway release.
"""

import grpc
from crossplane.function import logging, resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from crossplane.function.proto.v1 import run_function_pb2_grpc as grpcv1
from models.ai.modelplane.infrastructure.servingstack import v1alpha1
from models.io.crossplane.m.helm.providerconfig import v1beta1 as helmpcv1beta1
from models.io.crossplane.m.helm.release import v1beta1 as helmv1beta1
from models.io.crossplane.m.kubernetes.object import v1alpha1 as k8sobjv1alpha1
from models.io.crossplane.m.kubernetes.providerconfig import (
    v1alpha1 as k8spcv1alpha1,
)
from models.io.crossplane.protection.usage import v1beta1 as usagev1beta1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1

from function import gateway, stacks

# Label key every rendered Release and Object carries, valued with its
# composed-resource key, so Usage resourceSelectors can name any
# component (or one doc of a bundle) mechanically.
_LABEL_RESOURCE = "modelplane.ai/resource"

# Annotation provider-helm reads as the Helm release name. The stack
# lists carry the release name per Chart entry (mp-<chart>): stable
# across chart-version upgrades so provider-helm upgrades in place,
# short enough that chart-derived names stay inside the 63-character
# label limit, and mp- reserves a namespace so Modelplane can't adopt a
# same-named release a user already runs. See issue #215 and the
# design's "Ordering and identity".
_EXTERNAL_NAME_ANNOTATION = "crossplane.io/external-name"

# Secret type that names the kubeconfig entry in the XR's secrets. Every other
# entry's type is a provider identity type, which both ProviderConfigs stamp
# verbatim as their identity.type.
_SECRET_TYPE_KUBECONFIG = "Kubeconfig"

# The (apiVersion, kind) a component's composed resources render as,
# used by the derived Usages' of/by references.
_RELEASE_REF = ("helm.m.crossplane.io/v1beta1", "Release")
_OBJECT_REF = ("kubernetes.m.crossplane.io/v1alpha1", "Object")

# The cluster gateway's own PKI, issued by cert-manager, which the stack
# already installs. Composition functions are called repeatedly and must be a
# pure function of their inputs, so they can't generate key material; a
# controller has to. The private keys never leave this cluster.
#
# A self-signed issuer signs a CA, the CA signs the gateway's serving
# certificate, and the CA's certificate is published in status so an
# InferenceGateway can validate against it. One CA per cluster rather than one
# per fleet: no shared private key has to be distributed, and compromising one
# cluster doesn't let anyone impersonate another. The self-signed issuer and
# trust-manager are stack components (see common.py); the per-cluster chain
# below is hand-rendered because it's gated on the gateway hostname.
_CA_ISSUER = "modelplane-cluster-ca"
_CA_SECRET = "modelplane-cluster-ca"
_GATEWAY_SERVING_SECRET = "cluster-gateway-serving"

# The trust-manager Bundle republishing the CA certificate, and so also the
# ConfigMap it syncs, which is what the control plane reads. See
# compose_gateway_pki.
_CA_BUNDLE = "modelplane-cluster-ca"

# Where the CAs whose client certificates the gateway accepts are assembled.
_CLIENT_CA_BUNDLE = "modelplane-inference-gateway-cas"

# A cert-manager Certificate is Ready once it has issued.
_CERTIFICATE_READY_CEL = (
    "has(object.status) && has(object.status.conditions) && "
    "object.status.conditions.exists(c, c.type == 'Ready' && c.status == 'True')"
)

# A trust-manager Bundle is Synced once it has written its target ConfigMaps.
_BUNDLE_SYNCED_CEL = (
    "has(object.status) && has(object.status.conditions) && "
    "object.status.conditions.exists(c, c.type == 'Synced' && c.status == 'True')"
)

# A Gateway API policy reports acceptance per attachment, under status.ancestors
# rather than status.conditions. Envoy Gateway answers a ClientTrafficPolicy it
# can't translate by setting Accepted=False here and a 500 direct response on
# every route of the target listener, so a policy that isn't accepted takes the
# cluster gateway down rather than leaving it unprotected. Without this the
# Object reports ready on creation and the cluster looks healthy while every
# request fails.
_POLICY_ACCEPTED_CEL = (
    "has(object.status) && has(object.status.ancestors) && "
    "object.status.ancestors.exists(a, has(a.conditions) && "
    "a.conditions.exists(c, c.type == 'Accepted' && c.status == 'True'))"
)


def _name(meta: metav1.ObjectMeta | None) -> str:
    """The object's name, always set on resources read from the API server."""
    if meta is None or meta.name is None:
        raise ValueError("metadata.name is unexpectedly absent")
    return meta.name


def _namespace(meta: metav1.ObjectMeta | None) -> str:
    """The object's namespace, always set on namespaced resources read from the API server."""
    if meta is None or meta.namespace is None:
        raise ValueError("metadata.namespace is unexpectedly absent")
    return meta.namespace


def _helm_release(chart: stacks.Chart, provider_config: str) -> helmv1beta1.Release:
    """Build a Helm Release for a Chart entry, targeting the remote cluster."""
    release = helmv1beta1.Release(
        metadata=metav1.ObjectMeta(
            annotations={_EXTERNAL_NAME_ANNOTATION: chart.release},
            labels={_LABEL_RESOURCE: chart.key},
        ),
        spec=helmv1beta1.Spec(
            providerConfigRef=helmv1beta1.ProviderConfigRef(
                kind="ProviderConfig",
                name=provider_config,
            ),
            forProvider=helmv1beta1.ForProvider(
                chart=helmv1beta1.Chart(
                    name=chart.chart,
                    repository=chart.repository,
                    version=chart.version,
                ),
                namespace=chart.namespace,
            ),
        ),
    )
    if chart.wait:
        # Helm --wait: Ready means the workloads rolled out, so the
        # install gate orders dependents on health, not deploy. The
        # default 5m can be tight for the big monitoring charts on a
        # fresh cluster pulling images.
        release.spec.forProvider.wait = True
        release.spec.forProvider.waitTimeout = "10m"
    if chart.values:
        release.spec.forProvider.values = chart.values
    return release


def _k8s_object(
    provider_config: str,
    manifest: dict,
    metadata: metav1.ObjectMeta | None = None,
    *,
    ready_when: str | None = None,
    management_policies: list | None = None,
) -> k8sobjv1alpha1.Object:
    """Build a provider-kubernetes Object wrapping an arbitrary manifest.

    Readiness defaults to SuccessfulCreate (the Object is Ready once applied),
    which suits resources with no meaningful runtime readiness. Pass ready_when
    for an Object whose readiness must reflect a controller-populated field of
    the observed manifest - it selects the DeriveFromCelQuery policy with that
    query (see gateway.READY_CEL), which also keeps provider-kubernetes
    re-observing on its fast poll until the query passes.
    """
    obj = k8sobjv1alpha1.Object(
        # Only set metadata when present. Under exclude_unset serialization,
        # passing metadata=None would emit a null metadata into the composed
        # resource rather than omitting it.
        **({"metadata": metadata} if metadata is not None else {}),
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
    if management_policies:
        obj.spec.managementPolicies = management_policies
    if ready_when is not None:
        obj.spec.readiness = k8sobjv1alpha1.Readiness(
            policy="DeriveFromCelQuery",
            celQuery=ready_when,
        )
    return obj


def _usage(
    of_ref: tuple[str, str],
    of_key: str,
    by_ref: tuple[str, str],
    by_key: str,
) -> usagev1beta1.Usage:
    """Build a Usage holding `of` (a dependency) until `by` is gone."""
    return usagev1beta1.Usage(
        spec=usagev1beta1.Spec(
            of=usagev1beta1.Of(
                apiVersion=of_ref[0],
                kind=of_ref[1],
                resourceSelector=usagev1beta1.ResourceSelectorModel(
                    matchControllerRef=True,
                    matchLabels={_LABEL_RESOURCE: of_key},
                ),
            ),
            by=usagev1beta1.By(
                apiVersion=by_ref[0],
                kind=by_ref[1],
                resourceSelector=usagev1beta1.ResourceSelector(
                    matchControllerRef=True,
                    matchLabels={_LABEL_RESOURCE: by_key},
                ),
            ),
            replayDeletion=True,
        ),
    )


def _ensure_trailing_newline(cert: str) -> str:
    """A certificate ending in a newline, so several concatenate cleanly."""
    return cert if cert.endswith("\n") else cert + "\n"


def _pc_name(xr: v1alpha1.ServingStack) -> str:
    """Derive the ProviderConfig name from the XR."""
    return resource.child_name(_name(xr.metadata), "cluster")


class FunctionRunner(grpcv1.FunctionRunnerServiceServicer):
    """A FunctionRunner handles gRPC RunFunctionRequests."""

    def __init__(self) -> None:
        """Create a new FunctionRunner."""
        self.log = logging.get_logger()

    async def RunFunction(
        self, req: fnv1.RunFunctionRequest, _: grpc.aio.ServicerContext | None
    ) -> fnv1.RunFunctionResponse:  # ty: ignore[invalid-method-override]  # the generated grpc servicer base is untyped
        """Run the function."""
        log = self.log.bind(tag=req.meta.tag)
        log.info("Running function")

        rsp = response.to(req)
        c = Composer(req, rsp)
        c.compose()
        return rsp


class Composer:
    def __init__(self, req: fnv1.RunFunctionRequest, rsp: fnv1.RunFunctionResponse) -> None:
        self.req = req
        self.rsp = rsp
        self.xr = v1alpha1.ServingStack(**resource.struct_to_dict(req.observed.composite.resource))

    def compose(self) -> None:
        self.compose_provider_configs()

        # The XRD requires and enums both fields, so the join raising means the
        # API and the stacks package disagree on a value, a broken Modelplane
        # build, not a cluster condition. Let it crash rather than dress it up
        # as a fatal result.
        components = stacks.join(self.xr.spec.cloud, self.xr.spec.stack or "Standard")

        rendered = self.compose_components(components)
        rendered += self.compose_gateway()
        rendered += self.compose_gateway_pki()
        self.compose_component_usages(components)
        self.compose_gateway_usages()
        self.write_status()
        self.mark_readiness(rendered)

    def compose_provider_configs(self) -> None:
        """Build ProviderConfigs from the XR's secrets.

        The XRD requires a Kubeconfig secret, so one is always present.
        """
        xr_secrets = self.xr.spec.secrets or []

        kubeconfig_secret = next(s for s in xr_secrets if s.type == _SECRET_TYPE_KUBECONFIG)

        # The kubeconfig provides the cluster endpoint and CA cert. If an
        # identity secret is present, it's layered on as an identity block so the
        # provider authenticates via the cloud's IAM instead of relying on
        # whatever auth is baked into the kubeconfig.
        k8s_pc_spec = k8spcv1alpha1.Spec(
            credentials=k8spcv1alpha1.Credentials(
                source="Secret",
                secretRef=k8spcv1alpha1.SecretRef(
                    name=kubeconfig_secret.name,
                    namespace=_namespace(self.xr.metadata),
                    key=kubeconfig_secret.key,
                ),
            ),
        )
        helm_pc_spec = helmpcv1beta1.Spec(
            credentials=helmpcv1beta1.Credentials(
                source="Secret",
                secretRef=helmpcv1beta1.SecretRef(
                    name=kubeconfig_secret.name,
                    namespace=_namespace(self.xr.metadata),
                    key=kubeconfig_secret.key,
                ),
            ),
        )

        identity_secret = next(
            (s for s in xr_secrets if s.type != _SECRET_TYPE_KUBECONFIG),
            None,
        )
        if identity_secret:
            # The identity entry may carry its own namespace - the Nebius
            # credential is the Secret the Nebius ClusterProviderConfig
            # references, not one in this ServingStack's namespace.
            identity_namespace = identity_secret.namespace or _namespace(self.xr.metadata)
            k8s_pc_spec.identity = k8spcv1alpha1.Identity(
                type=identity_secret.type,  # ty: ignore[invalid-argument-type]  # non-Kubeconfig types are exactly the provider identity types
                source="Secret",
                secretRef=k8spcv1alpha1.SecretRef(
                    name=identity_secret.name,
                    namespace=identity_namespace,
                    key=identity_secret.key,
                ),
            )
            helm_pc_spec.identity = helmpcv1beta1.Identity(
                type=identity_secret.type,  # ty: ignore[invalid-argument-type]  # non-Kubeconfig types are exactly the provider identity types
                source="Secret",
                secretRef=helmpcv1beta1.SecretRef(
                    name=identity_secret.name,
                    namespace=identity_namespace,
                    key=identity_secret.key,
                ),
            )

        resource.update(
            self.rsp.desired.resources["provider-config-kubernetes"],
            k8spcv1alpha1.ProviderConfig(
                metadata=metav1.ObjectMeta(name=_pc_name(self.xr)),
                spec=k8s_pc_spec,
            ),
        )

        resource.update(
            self.rsp.desired.resources["provider-config-helm"],
            helmpcv1beta1.ProviderConfig(
                metadata=metav1.ObjectMeta(name=_pc_name(self.xr)),
                spec=helm_pc_spec,
            ),
        )

    def compose_components(self, components: list[stacks.Component]) -> list[str]:
        """Render every component of the joined stack.

        A Chart renders as one provider-helm Release under the entry's
        key; a Manifests entry as one provider-kubernetes Object per
        doc, keyed by stacks.components.doc_keys. Everything carries the
        _LABEL_RESOURCE label the derived Usages select on, and
        everything is gated on the ProviderConfigs being observed (see
        provider_configs_observed) so first creation doesn't race them.

        depends_on gates first creation too: a component is created
        only once every doc of every dependency reports Ready, so
        bring-up proceeds in dependency waves (cert-manager before the
        GPU Operator, the GPU Operator before the DRA driver). Once a
        resource exists it always re-composes - the observed check - so
        a dependency going unready later never deletes dependents. A
        Release reports Ready when Helm deploys it, not when its
        workloads run, so this is deploy-order, not health-order.

        Returns the composed-resource keys it rendered, for readiness.
        """
        pc_observed = self.provider_configs_observed()
        pc = _pc_name(self.xr)
        docs = {c.key: stacks.components.doc_keys(c) for c in components}

        def deps_ready(c: stacks.Component) -> bool:
            return all(
                resource.get_condition(self.req.observed.resources.get(key), "Ready").status == "True"
                for dep in c.depends_on
                for key in docs[dep]
            )

        rendered: list[str] = []
        for c in components:
            gate = pc_observed and deps_ready(c)
            if isinstance(c, stacks.Chart):
                if not (gate or c.key in self.req.observed.resources):
                    continue
                resource.update(self.rsp.desired.resources[c.key], _helm_release(c, pc))
                rendered.append(c.key)
                continue
            for key, doc in zip(stacks.components.doc_keys(c), c.manifests, strict=True):
                if not (gate or key in self.req.observed.resources):
                    continue
                resource.update(
                    self.rsp.desired.resources[key],
                    _k8s_object(
                        pc,
                        doc,
                        metadata=metav1.ObjectMeta(labels={_LABEL_RESOURCE: key}),
                        ready_when=c.ready,
                    ),
                )
                rendered.append(key)
        return rendered

    def compose_component_usages(self, components: list[stacks.Component]) -> None:
        """Derive teardown-ordering Usages from the components' edges.

        Crossplane applies composed resources concurrently, so without a
        Usage nothing sequences deletion. Each depends_on edge becomes
        one Usage per (dependency doc, dependent doc) pair, holding the
        dependency until the dependent is gone: the kai-scheduler
        release outlives the Queue CRs whose CRD it owns, cert-manager
        outlives the Envoy Gateway release whose webhooks need it, and
        so on. Usages reference nothing on the remote cluster, so they
        compose ungated and are ready on arrival.
        """
        refs: dict[str, tuple[str, str]] = {}
        docs: dict[str, list[str]] = {}
        for c in components:
            keys = stacks.components.doc_keys(c)
            docs[c.key] = keys
            for key in keys:
                refs[key] = _RELEASE_REF if isinstance(c, stacks.Chart) else _OBJECT_REF

        for c in components:
            for dep in c.depends_on:
                for of_key in docs[dep]:
                    for by_key in docs[c.key]:
                        key = f"usage-{of_key}-by-{by_key}"
                        resource.update(
                            self.rsp.desired.resources[key],
                            _usage(refs[of_key], of_key, refs[by_key], by_key),
                        )
                        self.rsp.desired.resources[key].ready = fnv1.READY_TRUE

    def serves_gateway(self) -> bool:
        """Whether this cluster's gateway should be serving.

        A cluster given a hostname is one InferenceGateways route to, over
        mutually authenticated HTTPS. Its gateway serves that or nothing, so it
        waits for at least one InferenceGateway CA to demand a client certificate
        against. A cluster with no hostname serves plain HTTP, is never
        schedulable, and nothing routes to it.
        """
        gw = self.xr.spec.gateway or v1alpha1.Gateway()
        return not gw.hostname or bool(gw.clientCAs or [])

    def compose_gateway(self) -> list[str]:
        """Compose the GatewayClass and Gateway on the remote cluster.

        The one hand-rendered pair, from function/gateway.py: both read
        spec.gateway, which stays per-cluster API rather than stack
        data. Gated on ProviderConfigs like every component.

        A cluster InferenceGateways route to (one given a hostname) serves the
        Gateway only once it has an InferenceGateway CA to demand a client
        certificate against; until then it composes the GatewayClass but
        withholds the Gateway, so nothing routes to it. The GatewayClass, the
        namespace and the EnvoyProxy
        (both stack components) are composed regardless, because the PKI and
        trust-manager live in that namespace and the gate is waiting on the CA
        they produce. See serves_gateway.

        Returns the composed-resource keys it rendered, for readiness.
        """
        pc_observed = self.provider_configs_observed()
        pc = _pc_name(self.xr)
        gw = self.xr.spec.gateway or v1alpha1.Gateway()
        serve_gateway = self.serves_gateway()
        if not serve_gateway:
            # Nothing else reports this. With no Gateway there is no address, so
            # the cluster publishes no hostname and every ModelDeployment
            # targeting it says only that it found insufficient capacity, which
            # points at the node pools rather than at the missing front door.
            response.warning(
                self.rsp,
                f"Gateway {gw.hostname} not served: no InferenceGateway has published a client CA for this "
                "cluster to trust, and serving without one would accept unauthenticated callers",
            )
        rendered: list[str] = []
        for key, manifest, cel in gateway.objects(self.xr.spec.gateway):
            if key == "gateway" and not serve_gateway:
                continue
            if not (pc_observed or key in self.req.observed.resources):
                continue
            resource.update(
                self.rsp.desired.resources[key],
                _k8s_object(
                    pc,
                    manifest,
                    metadata=metav1.ObjectMeta(labels={_LABEL_RESOURCE: key}),
                    ready_when=cel,
                ),
            )
            rendered.append(key)
        return rendered

    def compose_gateway_pki(self) -> list[str]:
        """Compose the cluster gateway's certificate, and the requirement that a
        caller present one of its own.

        Only once the cluster has a name: a certificate needs a subject, and a
        cluster with no name carries no traffic anyway, because an
        InferenceGateway addresses a cluster by name.

        cert-manager does the key generation, which a composition function can't:
        it runs on every reconcile and has to be a pure function of its inputs.

        The ClientTrafficPolicy is what makes an InferenceGateway the only thing
        that can reach the engines behind this gateway. Until at least one
        InferenceGateway has published a CA there is nothing to trust, and
        requiring a certificate signed by an empty set would refuse everything,
        so the requirement waits for the first one, and serves_gateway withholds
        the listener it would have governed until then.

        Returns the composed-resource keys it rendered, for readiness.
        """
        pc_observed = self.provider_configs_observed()
        pc = _pc_name(self.xr)
        gw = self.xr.spec.gateway or v1alpha1.Gateway()

        # The self-signed Issuer this chain roots in, and trust-manager which
        # republishes the CA it signs, are stack components composed on every
        # cluster (see stacks/common.py). The chain here is per-cluster: it
        # names the gateway hostname, so it waits for one.
        if not gw.hostname:
            return []

        rendered: list[str] = []
        certs: list[tuple[str, dict]] = [
            (
                "gateway-ca-certificate",
                {
                    "apiVersion": "cert-manager.io/v1",
                    "kind": "Certificate",
                    "metadata": {"name": _CA_ISSUER, "namespace": "modelplane-system"},
                    "spec": {
                        "isCA": True,
                        # Truncated to the 64-byte X.509 commonName limit: the
                        # gateway hostname is a full Service FQDN, so the prefix
                        # plus the name overflows it. Cosmetic anyway, since an
                        # InferenceGateway trusts this CA by its certificate and
                        # validates the serving one by SAN, not by this name.
                        "commonName": f"modelplane cluster CA {gw.hostname}"[:64],
                        "secretName": _CA_SECRET,
                        "duration": "87600h",
                        "renewBefore": "8760h",
                        "privateKey": {"algorithm": "ECDSA", "size": 256},
                        "issuerRef": {
                            "name": stacks.common.SELFSIGNED_ISSUER,
                            "kind": "Issuer",
                            "group": "cert-manager.io",
                        },
                    },
                },
            ),
            (
                "gateway-ca-issuer",
                {
                    "apiVersion": "cert-manager.io/v1",
                    "kind": "Issuer",
                    "metadata": {"name": _CA_ISSUER, "namespace": "modelplane-system"},
                    "spec": {"ca": {"secretName": _CA_SECRET}},
                },
            ),
            (
                "gateway-serving-certificate",
                {
                    "apiVersion": "cert-manager.io/v1",
                    "kind": "Certificate",
                    "metadata": {"name": _GATEWAY_SERVING_SECRET, "namespace": "modelplane-system"},
                    "spec": {
                        "secretName": _GATEWAY_SERVING_SECRET,
                        "dnsNames": [gw.hostname],
                        "duration": "2160h",
                        "renewBefore": "720h",
                        "privateKey": {"algorithm": "ECDSA", "size": 256, "rotationPolicy": "Always"},
                        "issuerRef": {"name": _CA_ISSUER, "kind": "Issuer", "group": "cert-manager.io"},
                    },
                },
            ),
        ]
        for key, manifest in certs:
            if not (pc_observed or key in self.req.observed.resources):
                continue
            cel = _CERTIFICATE_READY_CEL if manifest["kind"] == "Certificate" else None
            resource.update(self.rsp.desired.resources[key], _k8s_object(pc, manifest, ready_when=cel))
            rendered.append(key)

        # Republish the CA certificate on its own, so the control plane can read
        # it without reading the private key next to it.
        #
        # cert-manager writes ca.crt and tls.key into one Secret. Observing that
        # Secret would mean provider-kubernetes copying the whole thing into the
        # Object's status, private key included, where anyone who can get objects
        # could read it and mint a certificate any cluster would accept. Running
        # provider-kubernetes with --sanitize-secrets, as prerequisites.yaml
        # does, is no answer on its own: it redacts the data, so the read comes
        # back empty and mTLS is silently disabled instead.
        #
        # A Bundle takes one named key from a Secret and writes it to a ConfigMap,
        # so the key is read once, in-cluster, by a controller already entitled to
        # it. trust-manager also rejects any PEM block that isn't a CERTIFICATE,
        # so it can't be made to republish a key by naming the wrong source key.
        if pc_observed or "gateway-ca-bundle" in self.req.observed.resources:
            resource.update(
                self.rsp.desired.resources["gateway-ca-bundle"],
                _k8s_object(
                    pc,
                    {
                        "apiVersion": "trust.cert-manager.io/v1alpha1",
                        "kind": "Bundle",
                        # Cluster-scoped, and it names the ConfigMap it syncs.
                        "metadata": {"name": _CA_BUNDLE},
                        "spec": {
                            "sources": [{"secret": {"name": _CA_SECRET, "key": "ca.crt"}}],
                            "target": {
                                "configMap": {"key": "ca.crt"},
                                # A target syncs to every namespace by default.
                                # Only modelplane-system reads it.
                                "namespaceSelector": {
                                    "matchLabels": {"kubernetes.io/metadata.name": "modelplane-system"}
                                },
                            },
                        },
                    },
                    ready_when=_BUNDLE_SYNCED_CEL,
                ),
            )
            rendered.append("gateway-ca-bundle")

        # Observed, not managed: trust-manager owns this ConfigMap, and this only
        # needs to read the certificate back out so status can publish it.
        if pc_observed or "gateway-ca-configmap" in self.req.observed.resources:
            resource.update(
                self.rsp.desired.resources["gateway-ca-configmap"],
                _k8s_object(
                    pc,
                    {
                        "apiVersion": "v1",
                        "kind": "ConfigMap",
                        "metadata": {"name": _CA_BUNDLE, "namespace": "modelplane-system"},
                    },
                    management_policies=["Observe"],
                ),
            )
            rendered.append("gateway-ca-configmap")

        # With nothing to trust there is no HTTPS listener either (see
        # gateway.objects), so there is nothing to attach a policy to.
        client_cas = gw.clientCAs or []
        if not client_cas:
            return rendered
        if not (pc_observed or "gateway-client-ca-bundle" in self.req.observed.resources):
            return rendered
        # One ConfigMap holding every InferenceGateway's CA, concatenated, which
        # is what a PEM trust bundle is.
        resource.update(
            self.rsp.desired.resources["gateway-client-ca-bundle"],
            _k8s_object(
                pc,
                {
                    "apiVersion": "v1",
                    "kind": "ConfigMap",
                    "metadata": {"name": _CLIENT_CA_BUNDLE, "namespace": "modelplane-system"},
                    "data": {
                        "ca.crt": "".join(
                            _ensure_trailing_newline(ca.certificate) for ca in sorted(client_cas, key=lambda c: c.name)
                        )
                    },
                },
            ),
        )
        rendered.append("gateway-client-ca-bundle")
        resource.update(
            self.rsp.desired.resources["gateway-client-auth"],
            _k8s_object(
                pc,
                {
                    "apiVersion": "gateway.envoyproxy.io/v1alpha1",
                    "kind": "ClientTrafficPolicy",
                    "metadata": {"name": "cluster-gateway-client-auth", "namespace": "modelplane-system"},
                    "spec": {
                        "targetRefs": [
                            {
                                "group": "gateway.networking.k8s.io",
                                "kind": "Gateway",
                                "name": "cluster-gateway",
                                "sectionName": "https",
                            }
                        ],
                        "tls": {
                            "clientValidation": {
                                "caCertificateRefs": [{"kind": "ConfigMap", "group": "", "name": _CLIENT_CA_BUNDLE}]
                            }
                        },
                    },
                },
                ready_when=_POLICY_ACCEPTED_CEL,
            ),
        )
        rendered.append("gateway-client-auth")
        return rendered

    def compose_gateway_usages(self) -> None:
        """Compose Usages ordering the hand-rendered gateway teardown.

        The Envoy Gateway controller must outlive the Gateway and
        GatewayClass it manages: they carry finalizers it has to process
        on delete. The chain is Gateway Object -> GatewayClass Object ->
        envoy-gateway Release (a stack component, labelled by the
        renderer). These are hand-written because the gateway pair isn't
        stack data; every other ordering edge derives from depends_on.

        The GatewayClass-by-Gateway edge is composed only while there is a
        Gateway to be protected by. A Usage whose "by" selector matches
        nothing errors on every reconcile, and compose_gateway withholds the
        Gateway from a cluster with a hostname but no InferenceGateway CA to trust.
        """
        usages = [
            ("usage-envoy-gateway-by-gateway-class", _RELEASE_REF, "envoy-gateway", _OBJECT_REF, "gateway-class"),
        ]
        if self.serves_gateway():
            usages.insert(
                0,
                ("usage-gateway-class-by-gateway", _OBJECT_REF, "gateway-class", _OBJECT_REF, "gateway"),
            )
        for key, of_ref, of_key, by_ref, by_key in usages:
            resource.update(
                self.rsp.desired.resources[key],
                _usage(of_ref, of_key, by_ref, by_key),
            )
            self.rsp.desired.resources[key].ready = fnv1.READY_TRUE

    def observed_ca_certificate(self) -> str | None:
        """The cluster CA's certificate, read off the ConfigMap trust-manager
        syncs. A ConfigMap holds it as plain text, so unlike a Secret there is
        nothing to decode.

        Absent until cert-manager has issued and trust-manager has synced, which
        is why an InferenceGateway composes no backend for this cluster and the
        cluster publishes no hostname before then.
        """
        obj = self.req.observed.resources.get("gateway-ca-configmap")
        if obj is None:
            return None
        d = resource.struct_to_dict(obj.resource)
        data = d.get("status", {}).get("atProvider", {}).get("manifest", {}).get("data", {})
        return data.get("ca.crt") or None

    def write_status(self) -> None:
        """Extract the gateway address from the observed Gateway Object and
        write it to the XR's status."""
        gateway_address = None
        gateway_observed = self.req.observed.resources.get("gateway")
        if gateway_observed:
            gw_dict = resource.struct_to_dict(gateway_observed.resource)
            addresses = (
                gw_dict.get("status", {})
                .get("atProvider", {})
                .get("manifest", {})
                .get("status", {})
                .get("addresses", [])
            )
            if addresses:
                gateway_address = addresses[0].get("value")

        status = v1alpha1.Status()
        ca = self.observed_ca_certificate()
        if gateway_address or ca:
            status.gateway = v1alpha1.GatewayModel()
            if gateway_address:
                status.gateway.address = gateway_address
            if ca:
                status.gateway.caCertificate = ca
        resource.update_status(self.rsp.desired.composite, status)

    def mark_readiness(self, rendered: list[str]) -> None:
        """Mark composed resources as ready.

        The ProviderConfigs have no readiness condition of their own,
        but they must not be ready on arrival: on the first reconcile
        they and the Usages are the only desired resources, and marking
        them ready would let the composite report Ready before a single
        stack component exists. Observed - the same gate the rest of the
        stack opens on - is what makes them count. Everything rendered
        from the stack (and the gateway pair) is ready when its observed
        Ready condition is True - for Releases that's the Helm release
        deployed (its workloads rolled out, where the entry sets wait),
        for Objects the readiness policy (SuccessfulCreate, or the
        entry's CEL query).
        """
        for r in ("provider-config-kubernetes", "provider-config-helm"):
            if r in self.rsp.desired.resources and r in self.req.observed.resources:
                self.rsp.desired.resources[r].ready = fnv1.READY_TRUE

        for r in rendered:
            if resource.get_condition(self.req.observed.resources.get(r), "Ready").status == "True":
                self.rsp.desired.resources[r].ready = fnv1.READY_TRUE

    def provider_configs_observed(self) -> bool:
        """Check if both ProviderConfigs have been persisted by Crossplane from
        a previous reconcile. Resources targeting the remote cluster are gated
        on this to avoid transient 'ProviderConfig not found' errors on first
        creation."""
        return (
            "provider-config-helm" in self.req.observed.resources
            and "provider-config-kubernetes" in self.req.observed.resources
        )

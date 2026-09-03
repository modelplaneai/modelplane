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
    if chart.values:
        release.spec.forProvider.values = chart.values
    return release


def _k8s_object(
    provider_config: str,
    manifest: dict,
    metadata: metav1.ObjectMeta | None = None,
    *,
    cel_query: str | None = None,
) -> k8sobjv1alpha1.Object:
    """Build a provider-kubernetes Object wrapping an arbitrary manifest.

    Readiness defaults to SuccessfulCreate (the Object is Ready once applied),
    which suits resources with no meaningful runtime readiness. Pass cel_query
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
    if cel_query is not None:
        obj.spec.readiness = k8sobjv1alpha1.Readiness(
            policy="DeriveFromCelQuery",
            celQuery=cel_query,
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

        # The XRD requires and enums both fields, so the join can only
        # fail if the API and the stacks package disagree on a value -
        # a build defect worth a fatal result rather than a crash.
        try:
            components = stacks.components(self.xr.spec.cloud, self.xr.spec.stack or "Standard")
        except ValueError as err:
            response.fatal(self.rsp, str(err))
            return

        rendered = self.compose_components(components)
        rendered += self.compose_gateway()
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
        doc, keyed by stacks.doc_keys. Everything carries the
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
        docs = {c.key: stacks.doc_keys(c) for c in components}

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
            for key, doc in zip(stacks.doc_keys(c), c.manifests, strict=True):
                if not (gate or key in self.req.observed.resources):
                    continue
                resource.update(
                    self.rsp.desired.resources[key],
                    _k8s_object(
                        pc,
                        doc,
                        metadata=metav1.ObjectMeta(labels={_LABEL_RESOURCE: key}),
                        cel_query=c.ready,
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
            keys = stacks.doc_keys(c)
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

    def compose_gateway(self) -> list[str]:
        """Compose the GatewayClass and Gateway on the remote cluster.

        The one hand-rendered pair, from function/gateway.py: both read
        spec.gateway, which stays per-cluster API rather than stack
        data. Gated on ProviderConfigs like every component.

        Returns the composed-resource keys it rendered, for readiness.
        """
        pc_observed = self.provider_configs_observed()
        pc = _pc_name(self.xr)
        rendered: list[str] = []
        for key, manifest, cel in gateway.objects(self.xr.spec.gateway):
            if not (pc_observed or key in self.req.observed.resources):
                continue
            resource.update(
                self.rsp.desired.resources[key],
                _k8s_object(
                    pc,
                    manifest,
                    metadata=metav1.ObjectMeta(labels={_LABEL_RESOURCE: key}),
                    cel_query=cel,
                ),
            )
            rendered.append(key)
        return rendered

    def compose_gateway_usages(self) -> None:
        """Compose Usages ordering the hand-rendered gateway teardown.

        The Envoy Gateway controller must outlive the Gateway and
        GatewayClass it manages: they carry finalizers it has to process
        on delete. The chain is Gateway Object -> GatewayClass Object ->
        envoy-gateway Release (a stack component, labelled by the
        renderer). These are hand-written because the gateway pair isn't
        stack data; every other ordering edge derives from depends_on.
        """
        for key, of_ref, of_key, by_ref, by_key in (
            ("usage-gateway-class-by-gateway", _OBJECT_REF, "gateway-class", _OBJECT_REF, "gateway"),
            ("usage-envoy-gateway-by-gateway-class", _RELEASE_REF, "envoy-gateway", _OBJECT_REF, "gateway-class"),
        ):
            resource.update(
                self.rsp.desired.resources[key],
                _usage(of_ref, of_key, by_ref, by_key),
            )
            self.rsp.desired.resources[key].ready = fnv1.READY_TRUE

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
        if gateway_address:
            status.gateway = v1alpha1.GatewayModel(address=gateway_address)
        resource.update_status(self.rsp.desired.composite, status)

    def mark_readiness(self, rendered: list[str]) -> None:
        """Mark composed resources as ready.

        The ProviderConfigs have no readiness signal of their own, so
        they're ready on arrival. Everything rendered from the stack (and
        the gateway pair) is ready when its observed Ready condition is
        True - for Releases that's the Helm release deployed, for Objects
        the readiness policy (SuccessfulCreate, or the entry's CEL query).
        """
        for r in ("provider-config-kubernetes", "provider-config-helm"):
            if r in self.rsp.desired.resources:
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

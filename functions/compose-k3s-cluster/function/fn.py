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

"""Compose a k3s cluster onto existing machines over SSH.

This function installs k3s on machines the caller already has: the control
plane machine runs the k3s server, and each worker joins as a k3s agent.
provider-k3s drives the installs over SSH using a ProviderConfig that
carries the SSH user and private key.

The Cluster managed resource is composed first and publishes the cluster
kubeconfig as its connection secret. Node resources gate on the Cluster
being Ready: an agent can only join once the server is up and its join
token exists. Worker labels and taints are passed as k3s agent arguments,
so they are applied at node registration time.

Unlike the managed-Kubernetes cluster functions, no GPU observer gates
readiness here. Nothing preinstalls a GPU stack on bare machines; the
serving stack installs one after the cluster is Ready, so cluster
readiness cannot wait for it.
"""

import grpc
from crossplane.function import logging, resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from crossplane.function.proto.v1 import run_function_pb2_grpc as grpcv1
from models.ai.modelplane.infrastructure.k3scluster import v1alpha1
from models.io.crossplane.m.k3s.cluster import v1alpha1 as k3sclusterv1alpha1
from models.io.crossplane.m.k3s.node import v1alpha1 as k3snodev1alpha1
from models.io.crossplane.m.k3s.providerconfig import v1alpha1 as k3spcv1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1

# The k3s channel installed when the XR pins neither a channel nor a
# version: the first Kubernetes release where Dynamic Resource Allocation
# (how GPUs bind to pods) is generally available.
_DEFAULT_CHANNEL = "v1.34"

# Secret type written to XR status. compose-inference-cluster reads this to
# wire the kubeconfig into a ClusterProviderConfig.
_SECRET_TYPE_KUBECONFIG = "Kubeconfig"

# Key within the connection secret the Cluster resource writes. provider-k3s
# publishes the kubeconfig under this key once the server is installed.
_SECRET_KEY_KUBECONFIG = "kubeconfig"


def _name(meta: metav1.ObjectMeta | None) -> str:
    """The object's name, always set on resources read from the API server."""
    if meta is None or meta.name is None:
        raise ValueError("metadata.name is unexpectedly absent")
    return meta.name


def _namespace(meta: metav1.ObjectMeta | None) -> str:
    """The object's namespace, always set on resources read from the API server."""
    if meta is None or meta.namespace is None:
        raise ValueError("metadata.namespace is unexpectedly absent")
    return meta.namespace


def _kubeconfig_secret_name(xr: v1alpha1.K3sCluster) -> str:
    """Derive the kubeconfig secret name from the XR."""
    return resource.child_name(_name(xr.metadata), "kubeconfig")


def _provider_config_name(xr: v1alpha1.K3sCluster) -> str:
    """Derive the k3s ProviderConfig name from the XR."""
    return resource.child_name(_name(xr.metadata), "ssh")


def _cluster_name(xr: v1alpha1.K3sCluster) -> str:
    """Derive the Cluster managed resource name from the XR. Set explicitly
    so Node resources can reference it by name."""
    return resource.child_name(_name(xr.metadata), "cluster")


def _extra_args(worker: v1alpha1.Worker) -> str | None:
    """k3s agent arguments applying the worker's labels and taints at node
    registration time. Labels are sorted by key: the XR round-trips through
    protobuf structs, which don't preserve map order."""
    args = []
    for key, value in sorted((worker.labels or {}).items()):
        args.append(f"--node-label {key}={value}")
    for taint in worker.taints or []:
        spec = f"{taint.key}={taint.value}" if taint.value else taint.key
        args.append(f"--node-taint {spec}:{taint.effect}")
    return " ".join(args) if args else None


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
        self.xr = v1alpha1.K3sCluster(**resource.struct_to_dict(req.observed.composite.resource))

    def compose(self) -> None:
        self.compose_provider_config()
        self.compose_cluster()
        if self._cluster_ready() or self._dependents_observed():
            self.compose_nodes()
        self.write_status()
        self.mark_readiness()

    def _cluster_ready(self) -> bool:
        return resource.get_condition(self.req.observed.resources.get("cluster"), "Ready").status == "True"

    def _dependents_observed(self) -> bool:
        """Whether the Ready-gated Nodes were composed on a previous
        reconcile. The gate delays their first composition until the cluster
        is Ready, but must not drop them from desired state when the Ready
        condition transiently regresses - that would delete them, draining
        the joined agents from the cluster."""
        return any(name.startswith("node-") for name in self.req.observed.resources)

    def _channel(self) -> str | None:
        """The k3s channel to install, or None when an exact version is
        pinned instead."""
        version = self.xr.spec.version
        if version and version.version:
            return None
        if version and version.channel:
            return version.channel
        return _DEFAULT_CHANNEL

    def _version(self) -> str | None:
        version = self.xr.spec.version
        return version.version if version else None

    def _username(self) -> str:
        return self.xr.spec.auth.username or "root"

    def compose_provider_config(self) -> None:
        """Compose a k3s ProviderConfig carrying the SSH user and private
        key provider-k3s uses to reach every machine."""
        resource.update(
            self.rsp.desired.resources["provider-config-k3s"],
            k3spcv1alpha1.ProviderConfig(
                metadata=metav1.ObjectMeta(
                    name=_provider_config_name(self.xr),
                    namespace=_namespace(self.xr.metadata),
                ),
                spec=k3spcv1alpha1.Spec(
                    username=self._username(),
                    credentials=k3spcv1alpha1.Credentials(
                        source="Secret",
                        secretRef=k3spcv1alpha1.SecretRef(
                            namespace=_namespace(self.xr.metadata),
                            name=self.xr.spec.auth.secretRef.name,
                            key=self.xr.spec.auth.secretRef.key or "ssh-privatekey",
                        ),
                    ),
                ),
            ),
        )

    def compose_cluster(self) -> None:
        """Compose the Cluster that installs the k3s server on the control
        plane machine. Traefik is disabled - Envoy Gateway is the ingress -
        while the default ServiceLB stays on: it is what gives LoadBalancer
        Services an external IP on machines without a cloud load balancer."""
        cp = self.xr.spec.controlPlane
        fp = k3sclusterv1alpha1.ForProvider(
            host=cp.host,
            port=cp.port,
            tlsSAN=cp.host,
            disableTraefik=True,
        )
        # Only set the release field in use: an explicit None would still
        # serialize (resource.update dumps with exclude_unset) and clobber
        # the CRD's channel default.
        if self._channel():
            fp.k3sChannel = self._channel()
        if self._version():
            fp.k3sVersion = self._version()
        cluster = k3sclusterv1alpha1.Cluster(
            metadata=metav1.ObjectMeta(name=_cluster_name(self.xr)),
            spec=k3sclusterv1alpha1.Spec(
                providerConfigRef=k3sclusterv1alpha1.ProviderConfigRef(
                    kind="ProviderConfig",
                    name=_provider_config_name(self.xr),
                ),
                forProvider=fp,
                writeConnectionSecretToRef=k3sclusterv1alpha1.WriteConnectionSecretToRef(
                    name=_kubeconfig_secret_name(self.xr),
                ),
            ),
        )
        resource.update(self.rsp.desired.resources["cluster"], cluster)

    def compose_nodes(self) -> None:
        """Compose a Node joining each worker as a k3s agent. Gated on the
        cluster being Ready: an agent can only join once the server is up
        and its join token exists."""
        for worker in self.xr.spec.workers or []:
            fp = k3snodev1alpha1.ForProvider(
                host=worker.host,
                port=worker.port,
                role="agent",
                clusterRef=k3snodev1alpha1.ClusterRef(name=_cluster_name(self.xr)),
            )
            if self._channel():
                fp.k3sChannel = self._channel()
            if self._version():
                fp.k3sVersion = self._version()
            extra_args = _extra_args(worker)
            if extra_args:
                fp.extraArgs = extra_args
            node = k3snodev1alpha1.Node(
                spec=k3snodev1alpha1.Spec(
                    providerConfigRef=k3snodev1alpha1.ProviderConfigRef(
                        kind="ProviderConfig",
                        name=_provider_config_name(self.xr),
                    ),
                    forProvider=fp,
                ),
            )
            resource.update(self.rsp.desired.resources[f"node-{worker.name}"], node)

    def write_status(self) -> None:
        status = v1alpha1.Status(
            secrets=[
                v1alpha1.Secret(
                    type=_SECRET_TYPE_KUBECONFIG,
                    name=_kubeconfig_secret_name(self.xr),
                    key=_SECRET_KEY_KUBECONFIG,
                ),
            ],
        )
        resource.update_status(self.rsp.desired.composite, status)

    def mark_readiness(self) -> None:
        """Mark composed resources as ready based on their observed
        conditions. The ProviderConfig has no meaningful Ready condition and
        is always marked ready. The cluster and each node are marked ready
        only once their observed Ready condition is True, so the XR is Ready
        only when the server runs and every agent has joined."""
        for r in self.rsp.desired.resources:
            if r == "provider-config-k3s":
                self.rsp.desired.resources[r].ready = fnv1.READY_TRUE
                continue
            if resource.get_condition(self.req.observed.resources.get(r), "Ready").status == "True":
                self.rsp.desired.resources[r].ready = fnv1.READY_TRUE

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

"""Compose Vultr bare metal servers into a k3s cluster.

This function provisions one CPU-only management server and a fixed number
of GPU servers per node pool, then composes a K3sCluster XR that installs
k3s onto them over SSH: the management server becomes the k3s server, the
GPU servers join as agents.

The SSH key pair comes from a user-supplied Secret. The public key is
registered with Vultr as an SSHKey resource - every server selects it via
matchControllerRef, so Vultr installs it at provisioning time - and the
private key is handed to the K3sCluster for the installs. Nothing is
composed until the Secret resolves: servers provisioned without the key
would be unreachable.

The K3sCluster is composed only once every server is active and reports
its main IP; bare metal provisioning takes tens of minutes. Server IPs
come from Vultr resource observations, which persist for the life of the
server, so the K3sCluster's hosts stay stable across reconciles.

GPU pool workers are labelled for scheduling and tainted by GPU vendor
(amd.com/gpu or nvidia.com/gpu), derived from the accelerator type the
pool declares. Vultr's flagship bare metal GPU plans are AMD Instinct,
and the serving stack installs the AMD GPU operator on them.
"""

import base64

import grpc
from crossplane.function import logging, request, resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from crossplane.function.proto.v1 import run_function_pb2_grpc as grpcv1
from models.ai.modelplane.infrastructure.k3scluster import v1alpha1 as k3sv1alpha1
from models.ai.modelplane.infrastructure.vultrbaremetalcluster import v1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1
from models.io.upbound.m.vultr.compute.baremetalserver import v1beta1 as bmv1beta1
from models.io.upbound.m.vultr.compute.sshkey import v1beta1 as sshkeyv1beta1

# Labels written on worker nodes. compose-model-deployment reads these
# labels for GPU scheduling. Kept in sync with compose-vultr-cluster.
_LABEL_GPU = "modelplane.ai/gpu"
_LABEL_POOL = "modelplane.ai/pool"

# Taint applied to GPU workers so only inference workloads that tolerate
# GPUs are scheduled on them. The key follows the pool's GPU vendor,
# derived from the accelerator type the InferenceClass declares: Vultr
# offers both AMD Instinct and NVIDIA bare metal GPU plans.
_GPU_TAINT_KEY_AMD = "amd.com/gpu"
_GPU_TAINT_KEY_NVIDIA = "nvidia.com/gpu"
_GPU_TAINT_VALUE = "true"
_GPU_TAINT_EFFECT = "NoSchedule"

# Secret type written to XR status. compose-inference-cluster reads this to
# wire the kubeconfig into a ClusterProviderConfig.
_SECRET_TYPE_KUBECONFIG = "Kubeconfig"

# Vultr reports this once a bare metal server is provisioned and running.
_SERVER_STATUS_ACTIVE = "active"

# Cloud-init for every server. Vultr's Ubuntu images ship with UFW
# enabled and only SSH allowed, which lets the k3s install through (it
# runs over SSH) but firewalls everything the cluster itself needs:
# the API server (6443, agents joining and kubectl), the kubelet
# (10250), flannel's VXLAN overlay (8472/udp), and the gateway's HTTP
# ports served by k3s ServiceLB on the node IPs. The API server and
# kubelet authenticate with TLS client certificates; the VXLAN overlay
# rides the public network unauthenticated, which moving the data plane
# onto a Vultr VPC would fix.
_USER_DATA = """#cloud-config
runcmd:
- ufw allow 6443/tcp
- ufw allow 10250/tcp
- ufw allow 8472/udp
- ufw allow 80/tcp
- ufw allow 443/tcp
"""

# Defaults the XRD also declares. Coalesced here so the function tolerates
# an XR that predates server-side defaulting.
_DEFAULT_MANAGEMENT_PLAN = "vbm-6c-32gb-amd"
_DEFAULT_OS_ID = 2284
_DEFAULT_K3S_CHANNEL = "v1.34"
_DEFAULT_USERNAME = "root"
_DEFAULT_PRIVATE_KEY_KEY = "ssh-privatekey"
_DEFAULT_PUBLIC_KEY_KEY = "ssh-publickey"

# Condition type and reason set while the SSH key Secret is unresolved.
CONDITION_TYPE_CLUSTER_READY = "ClusterReady"
CONDITION_REASON_WAITING_FOR_SSH_SECRET = "WaitingForSSHSecret"

# Resource key of the management server; pool servers are keyed
# server-<pool>-<index>, which can't collide with it because a pool named
# management yields server-management-<index>.
_MANAGEMENT_SERVER_KEY = "server-management"


def _gpu_taint_key(accelerator_type: str) -> str:
    """The GPU taint key for a pool, by the vendor its accelerator type
    names (e.g. nvidia-h100 vs amd-mi355x)."""
    return _GPU_TAINT_KEY_NVIDIA if accelerator_type.startswith("nvidia") else _GPU_TAINT_KEY_AMD


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
        self.xr = v1alpha1.VultrBaremetalCluster(**resource.struct_to_dict(req.observed.composite.resource))

    def _cred_kind(self) -> str:
        creds = self.xr.spec.credentials
        return creds.type if creds and creds.type else "ClusterProviderConfig"

    def _cred_name(self) -> str:
        creds = self.xr.spec.credentials
        return creds.name if creds and creds.name else "default"

    def _management_plan(self) -> str:
        management = self.xr.spec.management
        return management.plan if management and management.plan else _DEFAULT_MANAGEMENT_PLAN

    def _management_os_id(self) -> int:
        management = self.xr.spec.management
        return management.osId if management and management.osId else _DEFAULT_OS_ID

    def _channel(self) -> str:
        k3s = self.xr.spec.k3s
        return k3s.channel if k3s and k3s.channel else _DEFAULT_K3S_CHANNEL

    def compose(self) -> None:
        public_key = self.resolve_public_key()
        if public_key is None:
            return

        self.compose_ssh_key(public_key)
        self.compose_servers()
        self.compose_k3s_cluster()
        self.write_status()
        self.mark_readiness()

    def resolve_public_key(self) -> str | None:
        """Declare and fetch the SSH key pair Secret, returning the public
        key. Returns None if the Secret or key is missing, in which case
        nothing is composed: servers provisioned without the key would be
        unreachable over SSH."""
        ssh = self.xr.spec.ssh
        response.require_resources(
            self.rsp,
            name="ssh-secret",
            api_version="v1",
            kind="Secret",
            match_name=ssh.secretRef.name,
            namespace=_namespace(self.xr.metadata),
        )

        secret = request.get_required_resource(self.req, "ssh-secret")
        if secret is None:
            msg = f"Waiting for SSH key Secret {ssh.secretRef.name}"
            response.set_conditions(
                self.rsp,
                resource.Condition(
                    typ=CONDITION_TYPE_CLUSTER_READY,
                    status="False",
                    reason=CONDITION_REASON_WAITING_FOR_SSH_SECRET,
                    message=msg,
                ),
            )
            response.normal(self.rsp, msg)
            return None

        key = ssh.secretRef.publicKeyKey or _DEFAULT_PUBLIC_KEY_KEY
        data = secret.get("data", {}).get(key)
        if not data:
            msg = f"SSH key Secret {ssh.secretRef.name} has no {key} key"
            response.set_conditions(
                self.rsp,
                resource.Condition(
                    typ=CONDITION_TYPE_CLUSTER_READY,
                    status="False",
                    reason=CONDITION_REASON_WAITING_FOR_SSH_SECRET,
                    message=msg,
                ),
            )
            response.warning(self.rsp, msg)
            return None

        return base64.b64decode(data).decode().strip()

    def compose_ssh_key(self, public_key: str) -> None:
        """Register the public key with Vultr. Servers select it via
        matchControllerRef, so Vultr installs it at provisioning time."""
        resource.update(
            self.rsp.desired.resources["ssh-key"],
            sshkeyv1beta1.SSHKey(
                spec=sshkeyv1beta1.Spec(
                    providerConfigRef=sshkeyv1beta1.ProviderConfigRef(
                        kind=self._cred_kind(),
                        name=self._cred_name(),
                    ),
                    forProvider=sshkeyv1beta1.ForProvider(
                        name=_name(self.xr.metadata),
                        sshKey=public_key,
                    ),
                ),
            ),
        )

    def compose_servers(self) -> None:
        """Compose the management server and each pool's servers."""
        name = _name(self.xr.metadata)
        resource.update(
            self.rsp.desired.resources[_MANAGEMENT_SERVER_KEY],
            self._server(f"{name}-management", self._management_plan(), self._management_os_id()),
        )
        for pool in self.xr.spec.nodePools:
            os_id = pool.osId if pool.osId else self._management_os_id()
            for i in range(pool.nodeCount or 1):
                resource.update(
                    self.rsp.desired.resources[f"server-{pool.name}-{i}"],
                    self._server(f"{name}-{pool.name}-{i}", pool.plan, os_id),
                )

    def _server(self, label: str, plan: str, os_id: int) -> bmv1beta1.BareMetalServer:
        return bmv1beta1.BareMetalServer(
            spec=bmv1beta1.Spec(
                providerConfigRef=bmv1beta1.ProviderConfigRef(
                    kind=self._cred_kind(),
                    name=self._cred_name(),
                ),
                forProvider=bmv1beta1.ForProvider(
                    label=label,
                    hostname=label,
                    plan=plan,
                    region=self.xr.spec.region,
                    osId=os_id,
                    sshKeyIdsSelector=bmv1beta1.SshKeyIdsSelector(matchControllerRef=True),
                    tags=[f"modelplane.ai/cluster={_name(self.xr.metadata)}"],
                    userData=_USER_DATA,
                ),
            ),
        )

    def _server_ip(self, key: str) -> str | None:
        """The observed main IP of an active server, or None while it is
        still provisioning."""
        observed = self.req.observed.resources.get(key)
        if observed is None:
            return None
        server = bmv1beta1.BareMetalServer.model_validate(resource.struct_to_dict(observed.resource))
        at = server.status.atProvider if server.status else None
        if at is None or at.status != _SERVER_STATUS_ACTIVE or not at.mainIp:
            return None
        return at.mainIp

    def compose_k3s_cluster(self) -> None:
        """Compose the K3sCluster once every server is active and reports
        its main IP. The management server becomes the k3s server; each GPU
        server joins as an agent labelled for its pool and tainted for GPU
        workloads."""
        management_ip = self._server_ip(_MANAGEMENT_SERVER_KEY)
        if management_ip is None:
            return

        workers: list[k3sv1alpha1.Worker] = []
        for pool in self.xr.spec.nodePools:
            for i in range(pool.nodeCount or 1):
                ip = self._server_ip(f"server-{pool.name}-{i}")
                if ip is None:
                    return
                workers.append(
                    k3sv1alpha1.Worker(
                        name=f"{pool.name}-{i}",
                        host=ip,
                        labels={
                            _LABEL_POOL: pool.name,
                            _LABEL_GPU: pool.gpu.acceleratorType,
                        },
                        taints=[
                            k3sv1alpha1.Taint(
                                key=_gpu_taint_key(pool.gpu.acceleratorType),
                                value=_GPU_TAINT_VALUE,
                                effect=_GPU_TAINT_EFFECT,
                            ),
                        ],
                    ),
                )

        ssh = self.xr.spec.ssh
        resource.update(
            self.rsp.desired.resources["k3s-cluster"],
            k3sv1alpha1.K3sCluster(
                spec=k3sv1alpha1.Spec(
                    controlPlane=k3sv1alpha1.ControlPlane(host=management_ip),
                    workers=workers,
                    auth=k3sv1alpha1.Auth(
                        username=ssh.username or _DEFAULT_USERNAME,
                        secretRef=k3sv1alpha1.SecretRef(
                            name=ssh.secretRef.name,
                            key=ssh.secretRef.privateKeyKey or _DEFAULT_PRIVATE_KEY_KEY,
                        ),
                    ),
                    version=k3sv1alpha1.Version(channel=self._channel()),
                ),
            ),
        )

    def write_status(self) -> None:
        """Relay the K3sCluster's published secrets. The kubeconfig secret
        name derives from the K3sCluster's generated name, so it is read
        from observation rather than derived here."""
        observed = self.req.observed.resources.get("k3s-cluster")
        if observed is None:
            return
        k3s = k3sv1alpha1.K3sCluster.model_validate(resource.struct_to_dict(observed.resource))
        if not k3s.status or not k3s.status.secrets:
            return
        resource.update_status(
            self.rsp.desired.composite,
            v1alpha1.Status(
                secrets=[v1alpha1.Secret(type=s.type, name=s.name, key=s.key) for s in k3s.status.secrets],
            ),
        )

    def mark_readiness(self) -> None:
        """Mark composed resources as ready based on their observed Ready
        conditions. The XR is Ready only once the SSHKey and every server
        are Ready and the K3sCluster reports the whole cluster up."""
        for r in self.rsp.desired.resources:
            if resource.get_condition(self.req.observed.resources.get(r), "Ready").status == "True":
                self.rsp.desired.resources[r].ready = fnv1.READY_TRUE

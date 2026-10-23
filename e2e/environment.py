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

"""Bring up, and tear down, the two kind clusters the end-to-end tests run on.

The workload cluster runs the serving stack, both gateways and the model. The
control plane runs Crossplane and the Configuration, managed by `crossplane
project run`, and registers the workload cluster as InferenceCluster local with
source: Existing. See README.md.

With --cloud the workload cluster plays a cloud's tainted GPU pool instead:
KWOK fakes the GPU nodes, and that cloud's ServingStack goes in rather than
the model manifests. The tests in e2e/clouds/ then audit placement.

    python -m e2e.environment up [--no-apply] [--cloud <cloud>]
    python -m e2e.environment down
"""

import argparse
import ipaddress
import json
import logging
import os
import pathlib
import shlex
import subprocess
import tempfile

from e2e import clouds

log = logging.getLogger(__name__)

ROOT = pathlib.Path(__file__).resolve().parent.parent

CONTROL_PLANE = "modelplane-e2e-local"
WORKLOAD = "modelplane-e2e-workload"
CONTROL_PLANE_CONTEXT = f"kind-{CONTROL_PLANE}"
WORKLOAD_CONTEXT = f"kind-{WORKLOAD}"

# Pinned so the workload cluster has the DRA APIs the serving stack's NVIDIA DRA
# driver needs (resource.k8s.io, GA in k8s 1.34). The control plane needs no DRA,
# so its image doesn't matter. v1.34.2 or newer: older kubelets deadlock on an
# idle DRA connection (k/k#133934).
WORKLOAD_NODE_IMAGE = "kindest/node:v1.34.8@sha256:02722c2dedddcfc00febf5d27fbeb9b7b2c14294c82109ff4a85d89ac9ba3256"
DEADLOCKING_KUBELETS = ("v1.34.0", "v1.34.1")

METALLB_URL = "https://raw.githubusercontent.com/metallb/metallb/v0.14.8/config/manifests/metallb-native.yaml"
CROSSPLANE_VERSION = "2.4.0"

# Cloud legs install KWOK for the fake GPU nodes; the model e2e doesn't use
# it. Applied from the release's plain manifests the way MetalLB is: kwok.yaml
# is the controller, stage-fast.yaml the Stages that keep fake nodes Ready and
# mark the pods scheduled onto them Running.
KWOK_URL = "https://github.com/kubernetes-sigs/kwok/releases/download/v0.8.0"

# The functions the ServingStack pipeline calls (see
# apis/servingstacks/composition.yaml). A cloud leg parks the rest.
SERVING_STACK_FUNCTIONS = ("compose-serving-stack", "compose-usages")

MANIFESTS = ROOT / "e2e" / "manifests"


class BringUpError(Exception):
    """The environment can't be brought up as asked."""


def up(*, apply_manifests: bool, cloud: clouds.Cloud | None = None) -> None:
    """Bring up both clusters and install Modelplane, then apply what the tests exercise.

    The model e2e applies the manifests in e2e/manifests. A cloud leg instead
    applies that cloud's fake GPU Nodes and its ServingStack, and parks the
    composition functions the ServingStack pipeline doesn't call.
    """
    up_workload(cloud=cloud)
    up_control_plane()
    if cloud is not None:
        park_unused_functions()
    if not apply_manifests:
        what = MANIFESTS if cloud is None else f"the {cloud.cloud} ServingStack"
        log.info("Control plane ready. Skipped applying %s", what)
        return
    if cloud is None:
        log.info("Applying %s", MANIFESTS)
        kubectl(CONTROL_PLANE_CONTEXT, "apply", f"--filename={MANIFESTS}")
        return
    log.info("Applying the fake device-pool nodes and the %s ServingStack", cloud.cloud)
    nodes = {"apiVersion": "v1", "kind": "List", "items": clouds.nodes(cloud)}
    kubectl(WORKLOAD_CONTEXT, "apply", "--filename=-", stdin=json.dumps(nodes))
    # Apply resets the fake nodes this leg shares with a previous one, but
    # never prunes whole objects: a leg with fewer pools reusing a cluster
    # (any leg after Existing's AMD pool) would keep stale fake nodes whose
    # taints the placement audits then reason about. Delete the leftovers.
    wanted = {node["metadata"]["name"] for node in clouds.nodes(cloud)}
    observed = output(
        "kubectl", f"--context={WORKLOAD_CONTEXT}",
        "get", "nodes", "--selector=type=kwok", "--output=json",
    )  # fmt: skip
    for stale in sorted({node["metadata"]["name"] for node in json.loads(observed)["items"]} - wanted):
        kubectl(WORKLOAD_CONTEXT, "delete", "node", stale)
    kubectl(CONTROL_PLANE_CONTEXT, "apply", "--filename=-", stdin=json.dumps(clouds.serving_stack(cloud)))


def up_workload(*, cloud: clouds.Cloud | None = None) -> None:
    """Create the workload cluster, and install what Modelplane expects a cluster to have already."""
    create_workload_cluster(cloud=cloud)

    # Both kind clusters share one Docker network. MetalLB hands out
    # LoadBalancer addresses from it, and the control plane must route to them,
    # so the pool has to sit inside the network's actual subnet. That's usually
    # 172.18.0.0/16, but kind moves to 172.19 and beyond when another Docker
    # network holds 172.18. The serving stack doesn't install MetalLB, and the
    # pool needs room for two Services, one per gateway.
    prefix = kind_subnet_prefix()
    log.info("Installing MetalLB on the workload cluster, with pool %s.255.100-149", prefix)
    kubectl(WORKLOAD_CONTEXT, "apply", f"--filename={METALLB_URL}")
    kubectl(WORKLOAD_CONTEXT, "rollout", "status", "--namespace=metallb-system", "deploy/controller", "--timeout=180s")
    pool = {
        "apiVersion": "v1",
        "kind": "List",
        "items": [
            {
                "apiVersion": "metallb.io/v1beta1",
                "kind": "IPAddressPool",
                "metadata": {"name": "kind-pool", "namespace": "metallb-system"},
                "spec": {"addresses": [f"{prefix}.255.100-{prefix}.255.149"]},
            },
            {
                "apiVersion": "metallb.io/v1beta1",
                "kind": "L2Advertisement",
                "metadata": {"name": "kind-l2", "namespace": "metallb-system"},
                "spec": {"ipAddressPools": ["kind-pool"]},
            },
        ],
    }
    kubectl(WORKLOAD_CONTEXT, "apply", "--filename=-", stdin=json.dumps(pool))

    if cloud is not None:
        # A cloud leg fakes the GPU pool instead of the GPU: KWOK manages the
        # Nodes bring-up applies later (shaped like the cloud's GPU pools, see
        # clouds/__init__.py), keeping them Ready and - via its stage-fast
        # Stages - marking every pod scheduled onto them Running. So the
        # stack's GPU DaemonSets and helm --wait behave as on a real tainted
        # cloud pool, while the real kind node stays untouched and plays the
        # untainted system pool.
        log.info("Installing KWOK (fake kubelets) on the workload cluster")
        kubectl(WORKLOAD_CONTEXT, "apply", f"--filename={KWOK_URL}/kwok.yaml")
        kubectl(WORKLOAD_CONTEXT, "apply", f"--filename={KWOK_URL}/stage-fast.yaml")
        kubectl(
            WORKLOAD_CONTEXT,
            "rollout", "status", "--namespace=kube-system", "deploy/kwok-controller", "--timeout=180s",
        )  # fmt: skip
        return

    # Fake DRA GPUs, so a `claim: DRA` engine's ResourceClaim binds on this
    # GPU-less node. Without a DRA driver the claim stays Pending and the engine
    # never schedules, and the fleet scheduler rejects an engine whose only
    # device is Synthetic.
    log.info("Installing dra-example-driver (fake GPUs) on the workload cluster")
    kubectl(WORKLOAD_CONTEXT, "apply", f"--filename={ROOT / 'e2e' / 'dra-example-driver.yaml'}")
    kubectl(
        WORKLOAD_CONTEXT,
        "rollout", "status", "--namespace=dra-example-driver", "ds/dra-example-driver-kubeletplugin", "--timeout=120s",
    )  # fmt: skip

    # Modelplane doesn't label a BYO cluster's nodes. The gpu-synthetic pool
    # selects on this.
    log.info("Labelling the workload node for pool gpu-synthetic")
    kubectl(
        WORKLOAD_CONTEXT,
        "label",
        "node",
        f"{WORKLOAD}-control-plane",
        "modelplane.ai/pool=gpu-synthetic",
        "--overwrite",
    )


def create_workload_cluster(*, cloud: clouds.Cloud | None = None) -> None:
    """Create the workload cluster, or reuse one whose version and bring-up mode match."""
    if WORKLOAD not in output("kind", "get", "clusters").split():
        log.info("Creating workload cluster %s (k8s v1.34, for DRA)", WORKLOAD)
        run("kind", "create", "cluster", f"--name={WORKLOAD}", f"--image={WORKLOAD_NODE_IMAGE}")
        return

    # An older cluster lacks the DRA APIs, and would fail the run confusingly
    # later. Only the real node's kubelet counts: a cluster a --cloud run left
    # behind also has the fake GPU nodes, whose kubelet is KWOK reporting a
    # kwok-* version.
    try:
        version = output(
            "kubectl", f"--context={WORKLOAD_CONTEXT}",
            "get", "nodes", "--selector=type!=kwok",
            "--output=jsonpath={.items[0].status.nodeInfo.kubeletVersion}",
        )  # fmt: skip
    except subprocess.CalledProcessError:
        version = "unreachable"
    if version in DEADLOCKING_KUBELETS:
        msg = (
            f"workload cluster {WORKLOAD} is {version}, whose kubelet deadlocks on an idle DRA connection "
            "(fixed in v1.34.2); recreate it with: nix run .#e2e -- --clean"
        )
        raise BringUpError(msg)
    if not version.startswith("v1.34."):
        msg = (
            f"workload cluster {WORKLOAD} is {version}, but v1.34 is required for the DRA APIs; "
            "recreate it with: nix run .#e2e -- --clean"
        )
        raise BringUpError(msg)
    check_workload_mode(cloud=cloud)
    log.info("Reusing workload cluster %s (%s)", WORKLOAD, version)


def check_workload_mode(*, cloud: clouds.Cloud | None) -> None:
    """Fail a reused workload cluster whose last bring-up conflicts with this one.

    The model e2e and a cloud leg can't share a cluster: reusing one for the
    other would put InferenceCluster local's serving stack and the leg's
    ServingStack side by side, colliding on release names, and park
    composition functions the model path calls. One leg reusing another's
    cluster needs no check: re-applying the fake nodes resets their labels and
    taint to the new leg's shape (apply prunes what the manifest dropped), and
    re-applying the ServingStack recomposes it for the new cloud.
    """
    if cloud is None:
        nodes = output(
            "kubectl", f"--context={WORKLOAD_CONTEXT}",
            "get", "nodes", "--selector=type=kwok", "--output=json",
        )  # fmt: skip
        if json.loads(nodes)["items"]:
            msg = (
                f"workload cluster {WORKLOAD} has fake GPU nodes from a --cloud run; "
                "recreate it with: nix run .#e2e -- --clean"
            )
            raise BringUpError(msg)
        return
    dra = output(
        "kubectl", f"--context={WORKLOAD_CONTEXT}",
        "get", "namespace", "dra-example-driver", "--ignore-not-found", "--output=name",
    )  # fmt: skip
    if dra:
        msg = (
            f"workload cluster {WORKLOAD} ran the model e2e (dra-example-driver is installed); "
            "recreate it with: nix run .#e2e -- --clean"
        )
        raise BringUpError(msg)


def kind_subnet_prefix() -> str:
    """Return the first two octets of the kind Docker network's IPv4 subnet."""
    subnets = output(
        "docker", "network", "inspect", "kind", "--format={{range .IPAM.Config}}{{println .Subnet}}{{end}}"
    )
    for subnet in subnets.split():
        network = ipaddress.ip_network(subnet)
        if isinstance(network, ipaddress.IPv4Network):
            log.info("kind Docker subnet is %s", network)
            return ".".join(str(network.network_address).split(".")[:2])
    msg = f"could not find an IPv4 subnet on the kind Docker network: {subnets!r}"
    raise BringUpError(msg)


def up_control_plane() -> None:
    """Build Modelplane and install it on a kind control plane, then register the workload cluster with it."""
    log.info("Building and running the control plane %s", CONTROL_PLANE)
    # The nix app runs with no system PATH, so a Docker config whose credsStore
    # is "desktop" would break package resolution. The provider packages are
    # public, so an empty config is enough.
    with tempfile.TemporaryDirectory() as docker_config:
        pathlib.Path(docker_config, "config.json").write_text("{}")
        # The lean control plane's narrowed MRAP goes in with --init-resources,
        # ahead of the providers, so the cloud providers stay dormant (safe-start
        # scales them to zero). prerequisites.yaml can't go the same way: it
        # opens with a comment-only YAML document, which `crossplane project run`
        # rejects and kubectl skips.
        run(
            "crossplane", "project", "run",
            f"--control-plane-name={CONTROL_PLANE}", "--cluster-admin", "--timeout=25m",
            f"--init-resources={ROOT / 'e2e' / 'lean-control-plane.yaml'}",
            f"--crossplane-version={CROSSPLANE_VERSION}",
            env={"DOCKER_CONFIG": docker_config},
        )  # fmt: skip

    # Finish the setup the install guide does by hand: apply the RBAC
    # prerequisites, then point the two providers at the DeploymentRuntimeConfigs
    # they define. The providers install before prerequisites.yaml, and an
    # ImageConfig binds only when a ProviderRevision is created, so provider-helm
    # would otherwise come up without the RBAC it grants, and
    # provider-kubernetes without --sanitize-secrets.
    log.info("Applying the prerequisites and the provider runtime configs")
    prerequisites = ROOT / "docs" / "manifests" / "install" / "prerequisites.yaml"
    kubectl(CONTROL_PLANE_CONTEXT, "apply", f"--filename={prerequisites}")
    for provider, runtime_config in (
        ("upbound-provider-helm", "provider-helm-modelplane"),
        ("upbound-provider-kubernetes", "provider-kubernetes-modelplane"),
    ):
        patch = {
            "spec": {
                "runtimeConfigRef": {
                    "apiVersion": "pkg.crossplane.io/v1beta1",
                    "kind": "DeploymentRuntimeConfig",
                    "name": runtime_config,
                }
            }
        }
        kubectl(
            CONTROL_PLANE_CONTEXT,
            "patch", f"provider.pkg.crossplane.io/{provider}", "--type=merge", f"--patch={json.dumps(patch)}",
        )  # fmt: skip

    # InferenceCluster local reads this kubeconfig to reach the workload
    # cluster. --internal gives the address on the kind network, which the
    # control plane's provider pods can reach and 127.0.0.1 isn't.
    # prerequisites.yaml creates its namespace.
    log.info("Registering the workload cluster's kubeconfig with the control plane")
    secret = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": "local-cluster-kubeconfig", "namespace": "modelplane-system"},
        "stringData": {"kubeconfig": output("kind", "get", "kubeconfig", "--internal", f"--name={WORKLOAD}")},
    }
    kubectl(CONTROL_PLANE_CONTEXT, "apply", "--filename=-", stdin=json.dumps(secret))


def park_unused_functions() -> None:
    """Scale the composition functions the ServingStack pipeline doesn't call to zero.

    A cloud leg composes exactly one XR, so parking the other functions gives
    the serving-stack install the runner's memory. A Deployment with zero
    desired replicas still reports Available, so the package revisions stay
    healthy - the same mechanism that keeps the safe-start cloud providers
    dormant. runtimeConfigRef binds like the provider patches in
    up_control_plane: the runtime reconciler applies it to the
    already-installed revision. The model e2e keeps every function: its path
    composes most of them.
    """
    log.info("Parking the composition functions the ServingStack pipeline doesn't call")
    config = {
        "apiVersion": "pkg.crossplane.io/v1beta1",
        "kind": "DeploymentRuntimeConfig",
        "metadata": {"name": "scaled-to-zero"},
        "spec": {"deploymentTemplate": {"spec": {"replicas": 0, "selector": {}, "template": {}}}},
    }
    kubectl(CONTROL_PLANE_CONTEXT, "apply", "--filename=-", stdin=json.dumps(config))
    patch = {
        "spec": {
            "runtimeConfigRef": {
                "apiVersion": "pkg.crossplane.io/v1beta1",
                "kind": "DeploymentRuntimeConfig",
                "name": "scaled-to-zero",
            }
        }
    }
    functions = output(
        "kubectl", f"--context={CONTROL_PLANE_CONTEXT}", "get", "functions.pkg.crossplane.io", "--output=name"
    ).split()
    for function in functions:
        # The installed names carry the project prefix
        # (modelplane-modelplanecompose-serving-stack), so match on the suffix.
        if function.endswith(SERVING_STACK_FUNCTIONS):
            continue
        kubectl(CONTROL_PLANE_CONTEXT, "patch", function, "--type=merge", f"--patch={json.dumps(patch)}")


def down() -> None:
    """Delete both clusters, and the control plane's local registry."""
    # Delete both clusters whatever project stop returns: it can exit 0 without
    # removing the cluster. It's here for the local registry it also manages.
    for cmd in (
        ("crossplane", "project", "stop", f"--control-plane-name={CONTROL_PLANE}"),
        ("kind", "delete", "cluster", f"--name={CONTROL_PLANE}"),
        ("kind", "delete", "cluster", f"--name={WORKLOAD}"),
        ("docker", "rm", "--force", f"{CONTROL_PLANE}-registry"),
    ):
        try:
            run(*cmd)
        except subprocess.CalledProcessError as e:
            log.warning("%s", e)


def kubectl(context: str, *args: str, stdin: str | None = None) -> None:
    """Run kubectl against a cluster, logging what it prints."""
    run("kubectl", f"--context={context}", *args, stdin=stdin)


def run(*cmd: str, stdin: str | None = None, env: dict[str, str] | None = None) -> None:
    """Run a command from the repository root, letting it print as it runs.

    Bring-up takes most of a CI run, so its progress shows live rather than
    being captured and shown only if it fails.
    """
    log.info("$ %s", shlex.join(cmd))
    subprocess.run(cmd, cwd=ROOT, env={**os.environ, **(env or {})}, input=stdin, text=True, check=True)


def output(*cmd: str) -> str:
    """Run a command from the repository root, and return what it wrote to stdout."""
    return subprocess.run(cmd, cwd=ROOT, stdout=subprocess.PIPE, text=True, check=True).stdout.strip()


def main() -> None:
    """Bring the environment up or down."""
    parser = argparse.ArgumentParser(prog="python -m e2e.environment", description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    up_command = commands.add_parser("up", help="bring up both clusters, install Modelplane and apply the manifests")
    up_command.add_argument("--no-apply", action="store_true", help="skip the manifests, to apply them by hand")
    up_command.add_argument(
        "--cloud",
        choices=sorted(clouds.CLOUDS),
        help="install this cloud's serving stack on KWOK-faked GPU pools instead of the model path",
    )
    commands.add_parser("down", help="delete both clusters")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if args.command == "down":
        down()
        return
    up(apply_manifests=not args.no_apply, cloud=clouds.CLOUDS[args.cloud] if args.cloud else None)


if __name__ == "__main__":
    main()

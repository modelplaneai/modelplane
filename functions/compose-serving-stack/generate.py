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

"""Generate the AICR-derived cloud halves of the serving stack.

The generator design/serving-stack-generation.md describes. It writes
one Python module per covered cloud into
function/stacks/clouds/generated/aicr/, each a COMPONENTS list of Chart
and Manifests entries that the function joins with the hand-written
common.py and the stack's own file (see function/stacks/__init__.py).

Per cloud, one `aicr recipe` at Modelplane's coordinate (service +
intent, os only where a recipe covers Modelplane's node image - see the
design's "Resolving Modelplane's coordinate") and one `aicr bundle`
with Modelplane's managed values forced via --set. The accelerator is
held out of the coordinate, so the generator also resolves once per
covered accelerator and unions the Kubernetes constraint floors,
failing if the strictest outruns the cloud cluster XRD's default.

This file is the whole pipeline: Modelplane's catalog - the registry
and the gke-cos overlay fork aicr's --data mechanism consumes - is
embedded below and written to a temporary directory per run, so
regenerating the stacks needs this one file and the pinned aicr on
PATH. GKE needs the fork because the `gpuStack` profile locks the
GPU-advertiser choice at every output boundary (0.20.0 also closed the
`bundle --set` path 0.18.0 let through), and neither upstream value
advertises through DRA; the fork adds a third value, `dra`, selected
with --profile gpuStack=dra.

Usage:

    nix run .#stacks [-- cloud ...]

With no arguments every cloud in CLOUDS regenerates. Classification
detail (drops, managed paths, dropped dependencies, manifests,
constraint floors) goes to stderr; the generated files carry provenance
in their header. Every component in a recipe must be classified in
ALLOW or DROP - an unknown component fails the run, else NVIDIA adding
a component would silently appear on every Modelplane cluster.

Bumping aicr
------------

aicr releases about every two weeks and its schemas are still
v1alpha*, so a bump is a review, not a chore. In order:

1. Update AICR_PIN here and `version` in nix/aicr.nix together, with
   hashes from the release's aicr_checksums.txt. The two must move in
   lockstep; check_pin() fails closed on a mismatch.
2. Re-sync GKE_COS_OVERLAY: diff it against the new tag's
   recipes/overlays/gke-cos.yaml and re-apply the one change (the dra
   profile value, the 1.35 floor, and the DRA selector paths on the
   stock values for union totality). The file is high-churn upstream.
   The fork is deleted the day NVIDIA/aicr#2515 or #2517 lands.
3. Expect ALLOW/DROP to fail closed on any component the new catalog
   adds; classify it with a reason.
4. Expect the managed-path assertions to fail closed if a values path
   moved or a --set stopped landing; re-derive the path from the new
   chart before loosening anything.
5. Both embedded catalog documents carry an apiVersion (v1alpha2 and
   v1alpha3 today) that can move in any release.
6. The Kubernetes floor union re-checks against `k8s_default` in
   CLOUDS, which must track the cloud cluster XRD defaults.
7. Regenerate, run twice to confirm no diff, and review the
   generated/aicr diff as the release's stack change.
"""

import ast
import hashlib
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any

import yaml

# The aicr release this file is synced against: check_pin() refuses any
# other, because GKE_COS_OVERLAY forks a file embedded in that exact
# release and the ALLOW/DROP tables classify that release's catalog.
# Moves together with `version` in nix/aicr.nix; see "Bumping aicr".
AICR_PIN = "0.20.0"

ROOT = pathlib.Path(__file__).parent
OUT = ROOT / "function" / "stacks" / "clouds" / "generated" / "aicr"

# Sentinel for "this path is not set at all" in lookups.
ABSENT = "<absent>"

Values = dict[str, Any]
ValuePath = tuple[str, ...]

# The Apache header addlicense expects on every .py file, emitted onto
# the generated files so `nix run .#fix` and the license check leave
# them alone. Byte-identical to the header addlicense writes.
LICENSE_HEADER = """\
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
# limitations under the License."""

# Modelplane's catalog, consumed via `aicr recipe --data`. Embedded so
# this one file produces the stacks; write_catalog() lays it out in the
# run's temporary directory.
REGISTRY_YAML = """\
# Required by aicr's layered data provider: an external --data directory
# must carry a registry.yaml, merged with the embedded one by component
# name. Modelplane contributes no components - the catalog exists only so
# overlays/gke-cos.yaml can replace the embedded file of the same name.
apiVersion: aicr.run/v1alpha2
kind: ComponentRegistry
components: []
"""

GKE_COS_OVERLAY = """\
# Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.  All rights reserved.
# Portions Copyright 2026 The Modelplane Authors.
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

# MODELPLANE FORK of aicr v0.20.0 recipes/overlays/gke-cos.yaml.
#
# aicr's --data mechanism replaces embedded catalog files wholesale by
# name, and that is the one supported way to change a configuration
# profile: the gpuStack profile is locked at every output boundary (a
# layered overlay can't move its paths, and 0.20.0 rejects `bundle
# --set` on them too), and neither upstream value advertises GPUs
# through DRA. Modelplane requires DRA, so this fork adds one profile
# value, `dra`, and changes nothing else. Union totality requires every
# value to own the same paths, so the two upstream values gain the DRA
# selector paths at their stock settings (explicit false).
#
# RE-SYNC ON EVERY aicr BUMP: diff this file against the pinned tag's
# recipes/overlays/gke-cos.yaml and re-apply the dra value. The file is
# high-churn upstream (on main, driver-installer has already become
# bundle-installer with an AICR-owned installer component). The fork is
# deleted the day upstream declares a DRA advertiser value - the asks
# are tracked in the design's "Remaining work".

kind: RecipeMetadata
apiVersion: aicr.run/v1alpha3
metadata:
  name: gke-cos

spec:
  criteria:
    service: gke
    os: cos

  constraints:
    # Upstream says >= 1.28. Raised here because DRA on GKE needs
    # Standard >= 1.35 (Google's set-up-dra guide), a profile value may
    # not restate a constraint name the composed recipe carries, and
    # this fork is only ever resolved with gpuStack=dra.
    - name: K8s.server.version
      value: ">= 1.35"

  # Upstream's gpuStack profile (ADR-015), plus Modelplane's dra value.
  # Upstream analysis this fork relies on: GKE's managed driver install
  # is finalized by an init container of the SAME kube-system DaemonSet
  # that the gke-no-default-nvidia-gpu-device-plugin=true label
  # disables, so a labeled pool comes up driverless and "GKE-installed
  # driver + another advertiser" is not a reachable state. Any value in
  # which GKE's managed plugin is NOT the advertiser therefore requires
  # the label and a driver supplied another way.
  profile:
    name: gpuStack
    description: >-
      GPU stack shape for GKE GPU node pools - how the driver is installed
      and which advertiser owns the GPUs. "gke-default" (the default) is
      the default-provisioned cluster: GKE's bundled driver install plus
      GKE's managed device plugin as the advertiser. "driver-installer"
      runs the GPU Operator's device plugin as the sole advertiser on
      labeled pools, with Google's standalone nvidia-driver-installer
      DaemonSet supplying the driver. "dra" (Modelplane's value) runs the
      NVIDIA DRA driver's ResourceSlices as the sole advertiser on the
      same labeled pool shape, with the driver supplied the same way.
    default: gke-default
    values:
      driver-installer:
        componentRefs:
          - name: gpu-operator
            overrides:
              devicePlugin:
                enabled: true
          - name: nvsentinel
            overrides:
              labeler: {assumeDriverInstalled: false}
          # Modelplane fork: DRA selector paths at stock (union totality).
          - name: nvidia-dra-driver-gpu
            overrides:
              gpuResourcesEnabledOverride: false
              resources:
                gpus: {enabled: false}
        constraints:
          - name: NodeTopology.gpu-nodes.label
            value: gke-no-default-nvidia-gpu-device-plugin=true
            remediation: >-
              The driver-installer profile requires the GPU Operator's device
              plugin to be the sole nvidia.com/gpu advertiser, so every GPU
              node pool must carry gke-no-default-nvidia-gpu-device-plugin=true
              at creation PLUS Google's standalone nvidia-driver-installer
              DaemonSet, with the pools created gpu-driver-version=disabled.
              See docs/integrator/gke-gpu-setup.md upstream. To use a
              default-provisioned GKE cluster instead, regenerate with the
              default (--profile gpuStack=gke-default).
      gke-default:
        advertiser: external
        componentRefs:
          - name: gpu-operator
            overrides:
              devicePlugin:
                enabled: false
          - name: nvsentinel
            overrides:
              labeler: {assumeDriverInstalled: true}
          # Modelplane fork: DRA selector paths at stock (union totality).
          - name: nvidia-dra-driver-gpu
            overrides:
              gpuResourcesEnabledOverride: false
              resources:
                gpus: {enabled: false}
        constraints:
          - name: NodeTopology.gpu-nodes.label
            value: "!gke-no-default-nvidia-gpu-device-plugin"
            remediation: >-
              The gke-default profile (the default) requires GKE's managed
              device plugin to be the sole nvidia.com/gpu advertiser, so NO
              GPU node may carry the opt-out label
              gke-no-default-nvidia-gpu-device-plugin. Remove the label, or
              regenerate with --profile gpuStack=driver-installer.

      # Modelplane's value: the NVIDIA DRA driver's ResourceSlices are
      # the sole GPU advertiser. The pool shape matches Google's own DRA
      # guide (docs.cloud.google.com/kubernetes-engine/docs/how-to/
      # set-up-dra): the opt-out label silences GKE's managed plugin
      # (else it would advertise nvidia.com/gpu beside the
      # ResourceSlices, two allocators over one device set), pools are
      # created with gpu-driver-version=disabled, and the driver is
      # installed separately (Google's standalone nvidia-driver-installer
      # DaemonSet) at /home/kubernetes/bin/nvidia. Google's guide also
      # sets gpuResourcesEnabledOverride and disables computeDomains -
      # the same values this profile owns. assumeDriverInstalled is true
      # because the bundler requires it whenever no bundle component
      # supplies a driver pod - it cannot see the standalone DaemonSet,
      # which is an operational prerequisite outside the bundle.
      dra:
        componentRefs:
          - name: gpu-operator
            overrides:
              devicePlugin:
                enabled: false
          - name: nvsentinel
            overrides:
              labeler: {assumeDriverInstalled: true}
          - name: nvidia-dra-driver-gpu
            overrides:
              gpuResourcesEnabledOverride: true
              resources:
                gpus: {enabled: true}
        constraints:
          # The 1.35 DRA floor lives in spec.constraints above: a profile
          # value may not restate a constraint name the composed recipe
          # already carries.
          - name: NodeTopology.gpu-nodes.label
            value: gke-no-default-nvidia-gpu-device-plugin=true
            remediation: >-
              The dra profile requires the NVIDIA DRA driver to be the sole
              GPU advertiser, so every GPU node pool must carry
              gke-no-default-nvidia-gpu-device-plugin=true at creation and
              be created with gpu-driver-version=disabled, with the driver
              installed separately (Google's standalone
              nvidia-driver-installer DaemonSet; the label forfeits GKE's
              managed driver install). Google's DRA guide additionally
              labels GPU pools nvidia.com/gpu.present=true and, for
              autoscaling, cloud.google.com/gke-nvidia-gpu-dra-driver=true.
              An unlabeled pool leaves GKE's managed device plugin
              advertising nvidia.com/gpu beside the DRA ResourceSlices -
              two allocators with independent ledgers over the same
              devices.

  # Upstream componentRefs, unchanged.
  componentRefs:
    - name: gpu-operator
      type: Helm
      valuesFile: components/gpu-operator/values-gke-cos.yaml

    - name: kube-prometheus-stack
      type: Helm
      overrides:
        prometheus:
          prometheusSpec:
            storageSpec:
              emptyDir: null
              volumeClaimTemplate:
                spec:
                  accessModes: ["ReadWriteOnce"]
                  resources:
                    requests:
                      storage: 50Gi

    - name: nvidia-dra-driver-gpu
      type: Helm
      overrides:
        nvidiaDriverRoot: /home/kubernetes/bin/nvidia
        controller:
          affinity:
            nodeAffinity: null

    - name: nodewright-operator
      type: Helm
      overrides:
        controllerManager:
          manager:
            env:
              copyDirRoot: /etc/nodewright
              reapplyOnReboot: "true"

  validation:
    conformance:
      checks:
        - platform-health
        - gpu-operator-health
        - dra-support
        - accelerator-metrics
        - ai-service-metrics
"""

# One generated module per cloud AICR has a `service` value for and
# Modelplane can bundle. `os` appears where a recipe covers the node
# image Modelplane uses: EKS leaves it unset because no recipe covers
# AL2023, and AKS refuses --os without an accelerator, so its cloud
# coordinate leaves it unset too. `accelerators` is the covered set the
# constraint-floor union resolves over; widening it is a line here and
# a regeneration. `k8s_default` is the cloud cluster XRD's default
# Kubernetes version - keep in sync with the XRDs. Nebius, Vultr, the
# AMD stack and Existing are hand-written files, not this generator's
# business; they live at the top of function/stacks/clouds/.
CLOUDS = {
    "eks": {
        "os": None,
        "accelerators": ["h100", "h200", "gb200", "rtx-pro-6000"],
        "k8s_default": "1.36",
    },
    "aks": {
        "os": None,
        "accelerators": ["h100"],
        "k8s_default": "1.34",
    },
    "gke": {
        "os": "cos",
        "accelerators": ["h100", "b200"],
        # DRA on GKE needs Standard >= 1.35 (Google's set-up-dra guide) -
        # a floor AICR doesn't carry because neither stock gpuStack value
        # allocates through DRA, and one the fork can't reliably inject:
        # later overlays in the chain restate K8s.server.version, and a
        # profile value may not restate a composed-recipe constraint
        # name. So Modelplane owns it here, in the union.
        "floors": ["1.35"],
        "k8s_default": "1.35",
    },
}

# aicr rejects its own wildcard toleration under AKS admission.
BUNDLE_EXTRA = {
    "aks": ["--accelerated-node-toleration", "nvidia.com/gpu=present:NoSchedule"],
}

# Recipe component name -> the key we emit it as. The key and the
# mp-prefixed release name are Modelplane's, not AICR's, so a
# component's identity survives upstream renames (see the design's
# "Ordering and identity").
ALLOW = {
    "nfd": "node-feature-discovery",
    "cert-manager": "cert-manager",
    "kube-prometheus-stack": "kube-prometheus-stack",
    "prometheus-operator-crds": "prometheus-operator-crds",
    "prometheus-adapter": "prometheus-adapter",
    "k8s-ephemeral-storage-metrics": "k8s-ephemeral-storage-metrics",
    "gpu-operator": "gpu-operator",
    "nvidia-dra-driver-gpu": "nvidia-dra-driver-gpu",
    "nvsentinel": "nvsentinel",
    "nodewright-operator": "nodewright-operator",
    "nodewright-customizations": "nodewright-customizations",
}

# In the recipe, deliberately not in the stack. Each entry needs a
# reason; an unclassified component fails the run. The allowlist fails
# closed.
DROP = {
    "agentgateway": "Modelplane routes through an InferencePool behind Envoy AI Gateway",
    "agentgateway-crds": "Modelplane routes through an InferencePool behind Envoy AI Gateway",
    "aws-efa": "the EKSCluster installs an EFA DRA driver when a pool asks for the fabric",
    "aws-ebs-csi-driver": "Modelplane's only storage need is RWX, which the EKSCluster serves from EFS",
    "network-operator": "the AKSCluster composition installs it when a pool asks for InfiniBand",
    "kai-scheduler": "contract surface, not hardware surface; Modelplane's pin beside its Queues, in stacks/dynamo.py",
}

# Long recipe-carried config blobs keep their content, not their
# cosmetics: dcgm-exporter's metrics CSV ships with blank spacer lines
# the exporter ignores. Trimming them keeps the generated file
# readable.
TRIM = {
    "gpu-operator": [("dcgmExporter", "config", "data")],
}

# Values Modelplane must own regardless of what a recipe says,
# mirroring the design's modelplane-*-inference overlay: DRA whole-GPU
# allocation over the device plugin (AICR rejects a partial flip), and
# the driver from the node image, which is Modelplane's mode on every
# cloud today ("The GPU driver" leans toward conforming to AICR's
# operator-installed default on EKS eventually; that lands here as a
# value change, not an API change). Turning the operator's driver off
# forces nvidiaDriverRoot to name where the node image put it, and
# forces NVSentinel's labeler to assume a driver it has no pod to
# observe - AICR's bundler refuses to render without both.
#
# This table is both applied and asserted: managed_sets() turns it
# into `aicr bundle --set` flags, and the transform verifies each
# value landed in the hydrated bundle. The assertion catches aicr
# silently ignoring a --set for a mistyped component key.
MANAGED = {
    "nvidia-dra-driver-gpu": [
        (("gpuResourcesEnabledOverride",), True, "Modelplane allocates GPUs via DRA ResourceSlices"),
        (("resources", "gpus", "enabled"), True, "Modelplane allocates GPUs via DRA ResourceSlices"),
        (("resources", "computeDomains", "enabled"), False, "multi-node NVLink unused"),
        (("nvidiaDriverRoot",), "/", "the node image puts the driver at the default root"),
    ],
    "gpu-operator": [
        (("driver", "enabled"), False, "the node image provides the kernel driver"),
        (("toolkit", "enabled"), False, "the node image provides the container toolkit"),
        (("devicePlugin", "enabled"), False, "the device plugin and DRA ResourceSlices double-advertise GPUs"),
        (("gdrcopy", "enabled"), False, "no operator-managed driver to build the kernel module against"),
    ],
    "nvsentinel": [
        (("labeler", "assumeDriverInstalled"), True, "no driver pod to observe with the operator's driver off"),
    ],
}

# Per-cloud expected values where a cloud legitimately differs.
MANAGED_CLOUD = {
    "gke": {
        # Google's installer puts the driver here under every gpuStack value.
        ("nvidia-dra-driver-gpu", ("nvidiaDriverRoot",)): "/home/kubernetes/bin/nvidia",
    },
}

# Paths the cloud's catalog data already sets - the gke fork's dra
# profile value owns the advertiser paths (aicr rejects a --set on a
# profile-owned path) and its overlay sets the driver root. Asserted in
# the hydrated bundle like every managed path, just not --set.
CATALOG_OWNED = {
    "gke": {
        ("nvidia-dra-driver-gpu", ("gpuResourcesEnabledOverride",)),
        ("nvidia-dra-driver-gpu", ("resources", "gpus", "enabled")),
        ("nvidia-dra-driver-gpu", ("nvidiaDriverRoot",)),
        ("gpu-operator", ("devicePlugin", "enabled")),
        ("nvsentinel", ("labeler", "assumeDriverInstalled")),
    },
}


def write_catalog(workdir: pathlib.Path) -> pathlib.Path:
    """Lay the embedded catalog out where `aicr recipe --data` reads it."""
    catalog = workdir / "catalog"
    (catalog / "overlays").mkdir(parents=True)
    (catalog / "registry.yaml").write_text(REGISTRY_YAML)
    (catalog / "overlays" / "gke-cos.yaml").write_text(GKE_COS_OVERLAY)
    return catalog


def recipe_extra(cloud: str, catalog: pathlib.Path) -> list[str]:
    """Extra `aicr recipe` flags a cloud needs.

    GKE resolves against Modelplane's catalog fork and its dra profile
    value (see the module docstring). Applied to every GKE recipe call,
    floor probes included, so the fork's union-totality and coherence
    checks run on each.
    """
    if cloud == "gke":
        return ["--data", str(catalog), "--profile", "gpuStack=dra"]
    return []


def managed(cloud: str) -> dict[str, list[tuple[ValuePath, object, str]]]:
    """The MANAGED table with per-cloud expected values substituted."""
    out = {}
    for component, entries in MANAGED.items():
        out[component] = [
            (path, MANAGED_CLOUD.get(cloud, {}).get((component, path), value), why) for path, value, why in entries
        ]
    return out


def deep_merge(base: Values | None, override: Values | None) -> Values:
    out = dict(base or {})
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def lookup(values: Values, path: ValuePath) -> object:
    """Read values[path], or ABSENT if any segment is missing."""
    node: object = values
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return ABSENT
        node = node[key]
    return node


def set_path(values: Values, path: ValuePath, value: object) -> None:
    node = values
    for key in path[:-1]:
        node = node.setdefault(key, {})
    node[path[-1]] = value


def version_key(version: str | None) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", version or "0"))


def managed_sets(cloud: str) -> list[str]:
    """Render the managed table as `aicr bundle --set` flags."""
    flags = []
    owned = CATALOG_OWNED.get(cloud, set())
    for component, entries in managed(cloud).items():
        for path, value, _ in entries:
            if (component, path) in owned:
                continue
            rendered = str(value).lower() if isinstance(value, bool) else str(value)
            flags += ["--set", f"{component}:{'.'.join(path)}={rendered}"]
    return flags


def bundle_values(bundle_dir: pathlib.Path, name: str) -> Values:
    """Load the hydrated values aicr bundle wrote for a component."""
    matches = sorted(bundle_dir.glob(f"[0-9][0-9][0-9]-{name}/values.yaml"))
    if not matches:
        return {}
    return yaml.safe_load(matches[0].read_text()) or {}


def k8s_floor(recipe: Values) -> tuple[list[str], list[str]]:
    """A recipe's Kubernetes floor, and its other constraints verbatim."""
    floors_found, others = [], []
    for constraint in recipe.get("constraints") or []:
        name, value = constraint.get("name", ""), str(constraint.get("value", ""))
        if name == "K8s.server.version":
            match = re.search(r"\d+(?:\.\d+)*", value)
            if not match:
                sys.exit(f"unparseable Kubernetes constraint {value!r} - fail closed")
            floors_found.append(match.group(0))
        else:
            others.append(f"{name} {value}".strip())
    return floors_found, others


def run(argv: list[str]) -> None:
    proc = subprocess.run(argv, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        sys.exit(f"{' '.join(argv)}\n{proc.stderr.strip()}")


def check_pin() -> None:
    """Refuse any aicr but the pinned release, before resolving anything."""
    if not shutil.which("aicr"):
        sys.exit("aicr not found on PATH (run via `nix run .#stacks`)")
    proc = subprocess.run(["aicr", "--version"], capture_output=True, text=True, check=False)
    match = re.search(r"aicr version (\S+)", proc.stdout)
    if not match:
        sys.exit(f"could not parse `aicr --version` output: {proc.stdout!r}")
    if match.group(1) != AICR_PIN:
        sys.exit(
            f"aicr {match.group(1)} on PATH, but this generator is synced "
            f'against {AICR_PIN}: follow "Bumping aicr" in generate.py\'s '
            "docstring (and update nix/aicr.nix) rather than mixing releases"
        )


def transform(cloud: str, recipe: Values, bundle_dir: pathlib.Path) -> tuple[list[Values], list[str]]:
    """Turn a hydrated recipe into Chart and Manifests entries."""
    findings = []
    components = []
    refs = {c["name"]: c for c in recipe["componentRefs"]}
    table = managed(cloud)
    owned = CATALOG_OWNED.get(cloud, set())

    for name in table:
        if name not in refs:
            findings.append(f"managed component {name} absent from the recipe")

    # AICR's install sequence is discarded as ordering (installs stay
    # concurrent; depends_on drives teardown) but kept as a stable
    # file order.
    for name in recipe["deploymentOrder"]:
        ref = refs[name]
        if name in DROP:
            findings.append(f"dropped: {name} ({DROP[name]})")
            continue
        if name not in ALLOW:
            sys.exit(f"unknown component {name!r}: classify it in ALLOW or DROP")
        key = ALLOW[name]

        values = deep_merge(bundle_values(bundle_dir, name), ref.get("overrides"))
        for path in TRIM.get(name, []):
            blob = lookup(values, path)
            if isinstance(blob, str):
                lines = [line.rstrip() for line in blob.splitlines()]
                set_path(values, path, "\n".join(line for line in lines if line) + "\n")
                findings.append(f"trimmed: {name}.{'.'.join(path)}: blank lines and trailing whitespace")
        for path, expected, why in table.get(name, []):
            dotted = f"{name}.{'.'.join(path)}"
            actual = lookup(values, path)
            if actual != expected:
                sys.exit(
                    f"managed path {dotted} is {actual!r}, expected "
                    f"{expected!r}: the value did not land in the bundle - fail closed"
                )
            via = "the catalog fork" if (name, path) in owned else "aicr bundle --set"
            findings.append(f"managed path: {dotted} = {expected} ({why}; via {via})")

        depends = []
        for dep in ref.get("dependencyRefs", []):
            if dep in ALLOW:
                depends.append(ALLOW[dep])
            elif dep in DROP:
                findings.append(
                    f"dropped dep: {name} -> {dep} (dropped; the join with "
                    "the hand-written files must satisfy it or nothing needed it)"
                )
            else:
                sys.exit(f"dependency {name} -> {dep!r} names an unclassified component - fail closed")

        entry = {
            "type": "Chart",
            "key": key,
            # provider-helm upgrades in place only while a release name
            # holds still; mp- reserves a namespace (see the design).
            "release": f"mp-{ref['chart']}",
            "namespace": ref["namespace"],
            "chart": ref["chart"],
            "repository": ref["source"],
            "version": ref["version"],
        }
        if depends:
            entry["depends_on"] = depends
        if values:
            entry["values"] = values
        components.append(entry)

        # Components are not always just charts: on AKS the
        # gpu-operator carries a toolkit-hardening manifest. The bundle
        # materializes them under <NNN>-<name>-post/templates/, and
        # they render as provider-kubernetes Objects.
        if ref.get("manifestFiles"):
            rendered = sorted(bundle_dir.glob(f"[0-9][0-9][0-9]-{name}-post/templates/*.yaml"))
            if not rendered:
                sys.exit(f"component {name!r} carries manifestFiles the bundle did not render - fail closed")
            manifests = []
            for f in rendered:
                manifests.extend(d for d in yaml.safe_load_all(f.read_text()) if d)
            findings.append(f"manifests: {name}: {len(manifests)} objects ({', '.join(f.name for f in rendered)})")
            components.append(
                {
                    "type": "Manifests",
                    "key": f"{key}-manifests",
                    "depends_on": [key],
                    "manifests": manifests,
                }
            )

    return components, findings


def floors(
    cloud: str,
    spec: Values,
    base_recipe: Values,
    workdir: pathlib.Path,
    catalog: pathlib.Path,
) -> tuple[str | None, list[str]]:
    """Union the Kubernetes floors over the covered accelerators.

    The cloud coordinate holds the accelerator out, which drops the
    accelerator's constraints - the strictest. Resolve once per covered
    accelerator, union the floors, and fail if the strictest outruns
    the cloud cluster XRD's default.
    """
    findings = []
    united, others = k8s_floor(base_recipe)
    for floor in spec.get("floors", []):
        united.append(floor)
        findings.append(f"Modelplane-owned floor {floor} (see the CLOUDS table)")
    for other in others:
        findings.append(f"constraint (unenforced here): {other}")
    for accelerator in spec["accelerators"]:
        recipe_file = workdir / f"{cloud}-{accelerator}-floor.yaml"
        flags = ["--service", cloud, "--accelerator", accelerator, "--intent", "inference"]
        if spec["os"]:
            flags += ["--os", spec["os"]]
        run(["aicr", "recipe", *flags, *recipe_extra(cloud, catalog), "-o", str(recipe_file)])
        got, others = k8s_floor(yaml.safe_load(recipe_file.read_text()))
        united += got
        for other in others:
            findings.append(f"constraint on {accelerator} (unenforced here): {other}")

    floor = max(united, key=version_key, default=None)
    if floor is None:
        findings.append("no Kubernetes floor asserted by any covered recipe")
        return None, findings
    if version_key(floor) > version_key(spec["k8s_default"]):
        sys.exit(
            f"{cloud}: strictest Kubernetes floor {floor} outruns the "
            f"cloud cluster XRD default {spec['k8s_default']} - fail closed"
        )
    findings.append(
        f"Kubernetes floor {floor} (union over no-accelerator, "
        f"{', '.join(spec['accelerators'])}); XRD default {spec['k8s_default']}"
    )
    return floor, findings


def pyfmt(value: object, indent: int) -> str:
    """Render a value as a Python literal, nested dicts one key per line."""
    pad = " " * indent
    if isinstance(value, dict):
        if not value:
            return "{}"
        lines = ["{"]
        for k, v in value.items():
            lines.append(f"{pad}    {k!r}: {pyfmt(v, indent + 4)},")
        lines.append(pad + "}")
        return "\n".join(lines)
    if isinstance(value, list):
        if not value:
            return "[]"
        lines = ["["]
        for item in value:
            lines.append(f"{pad}    {pyfmt(item, indent + 4)},")
        lines.append(pad + "]")
        return "\n".join(lines)
    if isinstance(value, str) and "\n" in value and '"""' not in value and not value.endswith('"'):
        return f'"""{value}"""'
    return repr(value)


def emit(
    cloud: str,
    spec: Values,
    components: list[Values],
    recipe: Values,
    digest: str,
    floor: str | None,
) -> None:
    fields = ["key", "release", "namespace", "chart", "repository", "version", "depends_on", "values", "manifests"]
    names = ["Chart", "Component"]
    if any(entry["type"] == "Manifests" for entry in components):
        names.append("Manifests")
    lines = [
        LICENSE_HEADER,
        "",
        f"# Generated by generate.py from aicr {recipe['metadata']['version']}. Do not edit.",
        "# Regenerate with `nix run .#stacks`.",
        "#",
        "# The AICR-derived cloud half: the function joins this list with the",
        "# hand-written common.py and the stack's own file (see",
        "# function/stacks/__init__.py).",
        "#",
        f"# Recipe: {' -> '.join(recipe['metadata']['appliedOverlays'])} (sha256:{digest[:12]})",
        f"# Kubernetes floor: {floor or 'none asserted'}; {cloud} XRD default {spec['k8s_default']}",
        "",
        f"from function.stacks.components import {', '.join(names)}",
        "",
        "COMPONENTS: list[Component] = [",
    ]
    for entry in components:
        lines.append(f"    {entry['type']}(")
        for field in fields:
            if field not in entry:
                continue
            lines.append(f"        {field}={pyfmt(entry[field], 8)},")
        lines.append("    ),")
    lines += ["]", ""]
    text = "\n".join(lines)
    ast.parse(text)  # a generated file that doesn't parse fails the build
    (OUT / f"{cloud}.py").write_text(text)


def regenerate(cloud: str, workdir: pathlib.Path, catalog: pathlib.Path) -> None:
    spec = CLOUDS[cloud]
    flags = ["--service", cloud, "--intent", "inference"]
    if spec["os"]:
        flags += ["--os", spec["os"]]
    flags += recipe_extra(cloud, catalog)
    recipe_file = workdir / f"{cloud}-inference.yaml"
    bundle_dir = workdir / f"{cloud}-bundle"

    run(["aicr", "recipe", *flags, "-o", str(recipe_file)])
    run(
        [
            "aicr",
            "bundle",
            "--recipe",
            str(recipe_file),
            "--output",
            str(bundle_dir),
            *managed_sets(cloud),
            *BUNDLE_EXTRA.get(cloud, []),
        ]
    )

    recipe = yaml.safe_load(recipe_file.read_text())
    components, findings = transform(cloud, recipe, bundle_dir)
    floor, floor_findings = floors(cloud, spec, recipe, workdir, catalog)
    for finding in findings + floor_findings:
        print(f"[{cloud}] {finding}", file=sys.stderr)

    digest = hashlib.sha256(recipe_file.read_bytes()).hexdigest()
    emit(cloud, spec, components, recipe, digest, floor)


def main(clouds: list[str]) -> None:
    unknown = [c for c in clouds if c not in CLOUDS]
    if unknown:
        sys.exit(f"unknown clouds {unknown}; known: {', '.join(CLOUDS)}")
    check_pin()
    OUT.mkdir(parents=True, exist_ok=True)
    # Recipes and bundles are intermediates: reproducible from the
    # pinned aicr's embedded catalog, digested into the file headers,
    # and not worth checking in.
    with tempfile.TemporaryDirectory(prefix="serving-stack-gen-") as tmp:
        workdir = pathlib.Path(tmp)
        catalog = write_catalog(workdir)
        for cloud in clouds or CLOUDS:
            regenerate(cloud, workdir, catalog)
            print(f"generated {OUT.relative_to(ROOT)}/{cloud}.py")


if __name__ == "__main__":
    main(sys.argv[1:])

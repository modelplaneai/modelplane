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
Beside them it writes dcgm.py, the DCGM exporter metrics CSV proven
identical across the clouds, for the hand-written clouds that run the
same exporter (see emit_dcgm).

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
GPU-advertiser choice at every output boundary (including `bundle
--set`), and neither upstream value advertises through DRA; the fork
adds a third value, `dra`, selected with --profile gpuStack=dra. The
profile-only --data patch aicr 1.0.0 added (aicr#2985) can't carry it -
see the fork's header.

Usage:

    nix run .#stacks [-- cloud ...]

With no arguments every cloud in CLOUDS regenerates. Classification
detail (drops, managed paths, dropped dependencies, manifests,
constraint floors) goes to stderr; the generated files carry provenance
in their header. Every component in a recipe must be classified in
ALLOW or DROP - an unknown component fails the run, else NVIDIA adding
a component would silently appear on every Modelplane cluster. Every
wildcard toleration the bundle stamps on a pod must likewise be
classified in TOLERATIONS, else that pod schedules onto tainted GPU
nodes (see the table).

Bumping the pinned aicr is a reviewed stack change - it moves the
components and versions every managed cluster runs on the next
Modelplane release. The procedure lives in CONTRIBUTING.md, "Bumping
aicr".
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
# Moves together with `version` in nix/aicr.nix; see CONTRIBUTING.md,
# "Bumping aicr".
AICR_PIN = "1.0.0"

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
apiVersion: aicr.run/v1beta1
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

# MODELPLANE FORK of aicr v1.0.0 recipes/overlays/gke-cos.yaml.
#
# aicr's --data mechanism replaces embedded catalog files wholesale by
# name, and that is the one supported way to add a configuration
# profile value that owns new paths: the gpuStack profile is locked at
# every output boundary (a layered overlay can't move its paths, and
# `bundle --set` on them is rejected), and neither upstream value
# advertises GPUs through DRA. v1.0.0's profile-only --data patch
# (aicr#2985) can't replace this fork: it is additive-only and a
# patched-in value must own exactly the embedded path set, while dra
# must own the DRA selector paths the upstream values don't - failing
# union totality by design, per the patch's own contract. So the fork
# adds one profile value, `dra`, gives the two upstream values the DRA
# selector paths at their stock settings (explicit false, union
# totality), raises the Kubernetes floor to DRA's, and changes nothing
# else.
#
# RE-SYNC ON EVERY aicr BUMP: diff this file against the pinned tag's
# recipes/overlays/gke-cos.yaml and re-apply the Modelplane changes
# (marked "Modelplane fork"). The file is high-churn upstream. The fork
# is deleted the day upstream declares a DRA advertiser value - the ask
# is NVIDIA/aicr#2517.

kind: RecipeMetadata
apiVersion: aicr.run/v1beta2
metadata:
  name: gke-cos

spec:
  criteria:
    service: gke
    os: cos

  constraints:
    # Modelplane fork: upstream says >= 1.32. Raised because DRA on GKE
    # needs Standard >= 1.35 (Google's set-up-dra guide), a profile
    # value may not restate a constraint name the composed recipe
    # carries, and this fork is only ever resolved with gpuStack=dra.
    - name: K8s.server.version
      value: ">= 1.35"

  # Upstream's gpuStack profile (ADR-015), plus Modelplane's dra value.
  # Upstream analysis this fork relies on: GKE's managed driver install
  # is finalized by an init container of the SAME kube-system DaemonSet
  # that the gke-no-default-nvidia-gpu-device-plugin=true label
  # disables, so a labeled pool comes up driverless and "GKE-installed
  # driver + another advertiser" is not a reachable state. Any value in
  # which GKE's managed plugin is NOT the advertiser therefore requires
  # the label and a driver supplied another way: bundle-installer
  # carries it in the bundle (the gcp-driver-installer component,
  # aicr#1716), dra takes Google's standalone nvidia-driver-installer
  # DaemonSet as an operational prerequisite outside the bundle.
  profile:
    name: gpuStack
    description: >-
      GPU stack shape for GKE GPU node pools - how the driver is installed
      and which advertiser owns the GPUs. The GPU Operator itself installs
      no driver on COS in any value. "gke-default" (the default) is the
      default-provisioned cluster: GKE's bundled driver install plus GKE's
      managed device plugin as the advertiser. "bundle-installer" is the
      opt-out pool shape (gke-no-default-nvidia-gpu-device-plugin=true,
      pools created with gpu-driver-version=disabled): the GPU Operator's
      device plugin is the sole advertiser, and the bundle-carried
      gcp-driver-installer component supplies the driver. "dra"
      (Modelplane's value) runs the NVIDIA DRA driver's ResourceSlices as
      the sole advertiser on the same labeled pool shape, with the driver
      supplied by Google's standalone nvidia-driver-installer DaemonSet.
    default: gke-default
    values:
      # Bundle-owned driver install (issue #1716): opt-out pools
      # (gpu-driver-version=disabled + the node label), with the
      # cos-gpu-installer DaemonSet carried by the gcp-driver-installer
      # component. The recipe pins the driver version; upgrades roll with
      # the bundle.
      bundle-installer:
        componentRefs:
          - name: gcp-driver-installer
            overrides:
              installer: {enabled: true}
          - name: gpu-operator
            overrides:
              devicePlugin:
                enabled: true
          # The bundle-carried installer IS a driver pod the NVSentinel
          # labeler detects, so no preinstalled-driver assumption.
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
              The bundle-installer profile requires the GPU Operator's
              device plugin to be the sole nvidia.com/gpu advertiser, so
              every GPU node pool must carry
              gke-no-default-nvidia-gpu-device-plugin=true at creation
              (gcloud container node-pools create ...
              --node-labels=gke-no-default-nvidia-gpu-device-plugin=true)
              and be created with gpu-driver-version=disabled - the label
              forfeits GKE's managed driver install, and the bundle's
              gcp-driver-installer component supplies the driver instead.
              See docs/integrator/gke-gpu-setup.md. To use a
              default-provisioned GKE cluster instead, regenerate with the
              default (--profile gpuStack=gke-default).
          - name: K8s.gke-gpu-pools.gpu-driver-installation
            value: Disabled
            remediation: >-
              The bundle-installer profile requires every GPU node pool to
              never have GKE's own managed driver install running, so it
              never races the bundle's own gcp-driver-installer component.
              This reading is satisfied by an explicit
              gpu-driver-version=disabled at creation, or by leaving the
              field unset on a GKE version whose documented default for
              that case is no install. A pool reporting Installed,
              NotConfigured, or NotInstalled has neither. An existing
              pool's driver-installation mode cannot be changed in place,
              so recreate any pool reporting otherwise with the explicit
              flag, which is the one fix that works regardless of GKE
              version (gcloud container node-pools create ...
              --accelerator type=<type>,count=<n>,gpu-driver-version=disabled).
              Then recapture the snapshot with
              --gke-gpu-pools pointed at a fresh `gcloud container
              node-pools list --cluster <cluster> --format=json` dump.
              See docs/integrator/gke-gpu-setup.md.

      # GKE-installed driver AND GKE's managed device plugin - a
      # default-provisioned GKE cluster (no node label required). The
      # external advertiser is counted as THE nvidia.com/gpu advertiser in
      # the #1327 exactly-one invariant; the resolved policy value stays
      # device-plugin-extended-resource (the policy names the request
      # mechanism, not provider ownership).
      gke-default:
        advertiser: external
        componentRefs:
          - name: gcp-driver-installer
            overrides:
              installer: {enabled: false}
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
              regenerate with --profile gpuStack=bundle-installer.

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
      # the same values this profile owns. The bundle-carried
      # gcp-driver-installer stays off: its DaemonSet shares the
      # standalone one's name and Helm will not adopt the pre-existing
      # object, so flipping existing clusters to the bundle-carried
      # driver is a separate, operational change, not a bump side
      # effect. assumeDriverInstalled is true because the bundler
      # requires it whenever no bundle component supplies a driver pod -
      # it cannot see the standalone DaemonSet, which is an operational
      # prerequisite outside the bundle.
      dra:
        componentRefs:
          - name: gcp-driver-installer
            overrides:
              installer: {enabled: false}
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
    # Values-gated driver installer (issue #1716): present unconditionally so
    # the gpuStack profile may own its installer.enabled gate (a profile can
    # change how components are configured, never the component set). Renders
    # nothing except under bundle-installer, where it supplies the driver.
    - name: gcp-driver-installer
      type: Helm
      valuesFile: components/gcp-driver-installer/values.yaml

    - name: gpu-operator
      type: Helm
      valuesFile: components/gpu-operator/values-gke-cos.yaml
      # Driver must land before GPU Operator operands start. A no-op
      # dependency under gke-default (empty release).
      dependencyRefs:
        - gcp-driver-installer

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

# aicr rejects its own wildcard toleration under AKS admission, so the
# AKS bundle takes an explicit one. The value must match the taint
# compose-aks-cluster puts on GPU pools (_GPU_TAINT, nvidia.com/gpu=
# true) - an Equal toleration with any other value excludes every
# accelerated daemonset (NFD's worker first, and with it the label the
# DRA kubelet plugin schedules on) from exactly the nodes it exists
# for.
BUNDLE_EXTRA = {
    "aks": ["--accelerated-node-toleration", "nvidia.com/gpu=true:NoSchedule"],
}

# The bundler stamps a wildcard toleration - a bare {operator: Exists},
# tolerating every taint - onto every pod it renders, Deployments
# included. On a Modelplane cluster that puts cert-manager, the
# Prometheus stack and the rest on GPU nodes: the cluster compositions
# taint GPU pools (nvidia.com/gpu=true:NoSchedule on EKS and AKS; GKE
# taints its GPU pools itself) exactly so that only GPU workloads land
# there, and a tolerate-everything pod squats on accelerated capacity
# and stalls autoscaler scale-down. So the transform rewrites every
# wildcard the bundle ships, per the table below: node-scoped workloads
# that must reach GPU nodes tolerate exactly the GPU taint (GPU,
# matching the hand-written stacks' NFD worker and the EKS
# composition's EFA driver), node-scoped workloads that must observe
# every inference node tolerate every inference pool taint (NODE), and
# everything else tolerates nothing (SYSTEM) and schedules on the
# untainted system pool. A toleration the bundle already scoped (AKS's
# --accelerated-node-toleration form above) passes through untouched. The wildcard doesn't only arrive under keys
# named tolerations: nvsentinel's bundle puts one under
# global.systemNodeTolerations, so the walkers match any *Tolerations
# key too.
WILDCARD_TOLERATION = [{"operator": "Exists"}]


def tolerations_key(key: str) -> bool:
    """Return whether a values or manifest key holds a toleration list."""
    return key == "tolerations" or key.endswith("Tolerations")


# Tolerate nothing: schedule on the untainted system pool only.
SYSTEM: list[Values] = []

# Tolerate exactly the GPU taint, whatever its value (true on EKS and
# AKS; GKE's own taint carries present).
GPU: list[Values] = [{"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"}]

# Tolerate every schedulable pool's taint - GPU and CPU alike - for
# node-reaching DaemonSets that aren't part of any one device stack and
# must observe every node Modelplane schedules models onto.
NODE: list[Values] = [
    {"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"},
    {"key": "modelplane.ai/cpu", "operator": "Exists", "effect": "NoSchedule"},
]

# Recipe component name -> dotted values path -> the tolerations that
# replace a wildcard found there. Like ALLOW/DROP this fails closed: a
# wildcard at an unclassified path fails the run, else an aicr bump
# could put a new control-plane pod back on GPU nodes.
TOLERATIONS = {
    "cert-manager": {
        "tolerations": SYSTEM,
        "cainjector.tolerations": SYSTEM,
        "webhook.tolerations": SYSTEM,
        "startupapicheck.tolerations": SYSTEM,
    },
    "nfd": {
        # The worker and topology updater must label GPU nodes - the
        # DRA kubelet plugin schedules on a label the worker applies
        # (see BUNDLE_EXTRA).
        "worker.tolerations": GPU,
        "topologyUpdater.tolerations": GPU,
        "master.tolerations": SYSTEM,
        "gc.tolerations": SYSTEM,
    },
    "kube-prometheus-stack": {
        "prometheus-node-exporter.tolerations": NODE,
        "alertmanager.alertmanagerSpec.tolerations": SYSTEM,
        "grafana.tolerations": SYSTEM,
        "kube-state-metrics.tolerations": SYSTEM,
        "prometheus.prometheusSpec.tolerations": SYSTEM,
        "prometheusOperator.tolerations": SYSTEM,
        "prometheusOperator.admissionWebhooks.patch.tolerations": SYSTEM,
        "thanosRuler.thanosRulerSpec.tolerations": SYSTEM,
    },
    "prometheus-adapter": {
        "tolerations": SYSTEM,
    },
    "k8s-ephemeral-storage-metrics": {
        # deploy_type is Deployment: one controller reads every node's
        # metrics from the API, so it never needs to reach GPU nodes.
        "tolerations": SYSTEM,
    },
    "gpu-operator": {
        "daemonsets.tolerations": GPU,
        "operator.tolerations": SYSTEM,
    },
    "nvidia-dra-driver-gpu": {
        "kubeletPlugin.tolerations": GPU,
        "controller.tolerations": SYSTEM,
    },
    "nvsentinel": {
        # The chart splits its global knob in two: tolerations goes to
        # the node health monitors (DaemonSets that must reach GPU
        # nodes), systemNodeTolerations to its control plane (the
        # labeler Deployment), which belongs on the system pool.
        "global.tolerations": GPU,
        "global.systemNodeTolerations": SYSTEM,
    },
    "nodewright-operator": {
        "controllerManager.tolerations": SYSTEM,
    },
}

# Same classification for the bundle's rendered manifests, keyed by
# (recipe component, kind, name); the tolerations apply to every
# wildcard in the manifest. The toolkit-hardening DaemonSet exists to
# reconfigure containerd on GPU nodes.
MANIFEST_TOLERATIONS = {
    ("gpu-operator", "DaemonSet", "nvidia-toolkit-hardening"): GPU,
}

# Recipe component name -> (the key we emit it as, the DRA drivers
# whose device stacks it belongs to). The key and the mp-prefixed
# release name are Modelplane's, not AICR's, so a component's identity
# survives upstream renames (see the design's "Ordering and identity").
# The driver list fails closed like the allowlist itself: classifying a
# new component includes deciding which stacks it serves, the review a
# bump already requires - the tag just records it. What the tag means
# for installs lives on Component.dra_drivers and join() in the stacks
# package (and in design/multi-accelerator.md).
ALLOW = {
    "nfd": ("node-feature-discovery", ["gpu.nvidia.com"]),
    "cert-manager": ("cert-manager", []),
    "kube-prometheus-stack": ("kube-prometheus-stack", []),
    "prometheus-operator-crds": ("prometheus-operator-crds", []),
    "prometheus-adapter": ("prometheus-adapter", []),
    "k8s-ephemeral-storage-metrics": ("k8s-ephemeral-storage-metrics", []),
    "gpu-operator": ("gpu-operator", ["gpu.nvidia.com"]),
    "nvidia-dra-driver-gpu": ("nvidia-dra-driver-gpu", ["gpu.nvidia.com"]),
    "nvsentinel": ("nvsentinel", ["gpu.nvidia.com"]),
    "nodewright-operator": ("nodewright-operator", ["gpu.nvidia.com"]),
    "nodewright-customizations": ("nodewright-customizations", ["gpu.nvidia.com"]),
}

# In the recipe, deliberately not in the stack. Each entry needs a
# reason; an unclassified component fails the run. The allowlist fails
# closed.
DROP = {
    "gcp-driver-installer": (
        "values-gated to render nothing under gpuStack=dra; Modelplane's GKE driver "
        "is Google's standalone nvidia-driver-installer DaemonSet, an operational "
        "prerequisite outside the stack (see the fork's dra value)"
    ),
    "agentgateway": "Modelplane routes through an InferencePool behind Envoy AI Gateway",
    "agentgateway-crds": "Modelplane routes through an InferencePool behind Envoy AI Gateway",
    "dra-node-labeler": (
        "serves the opt-in DRA driver-upgrade eviction label "
        "(--dra-eviction-node-label), which Modelplane doesn't opt into; the "
        "bundler drops it from the bundle without the flag"
    ),
    "aws-efa": "the EKSCluster installs an EFA DRA driver when a pool asks for the fabric",
    "aws-ebs-csi-driver": "Modelplane's only storage need is RWX, which the EKSCluster serves from EFS",
    "network-operator": "the AKSCluster composition installs it when a pool asks for InfiniBand",
    "kai-scheduler": "contract surface, not hardware surface; Modelplane's pin beside its Queues, in stacks/dynamo.py",
}

# Component key -> the namespace the release already occupies on
# existing clusters. Helm release identity is (name, namespace), and
# provider-helm upgrades in place only while both hold still: on a
# catalog-side namespace move it finds nothing at the new coordinates
# and runs a fresh install, which then fails on the
# meta.helm.sh/release-namespace ownership annotations the old release
# left on cluster-scoped objects - CRDs, ClusterRoles, webhook
# configurations (NVIDIA/aicr#2899) - wedging every existing cluster
# while CI, which only sees fresh installs, stays green. So a release's
# namespace stays where existing clusters have it; moving one is a
# documented migration, never a bump side effect.
# check_namespace_holds() fails the run on any unpinned move.
NAMESPACE_PINS = {
    # nodewright 0.19.0 renamed its chart namespace from skyhook (the
    # project's former name) to nodewright; Modelplane's releases live
    # in skyhook.
    "nodewright-operator": "skyhook",
}

# DRA drivers a cloud supports that arrive outside the serving stack,
# emitted into the generated module so the stacks package's derived
# supported-drivers table can't drift from the generated file. EKS's
# dra.net is the aws-efa DROP entry above as the filter sees it:
# compose-eks-cluster installs the EFA DRA driver when a pool asks for
# the fabric, and the kimi-k2 and qwen3-coder recipes claim it today.
EXTERNAL_DRIVERS = {
    "eks": ["dra.net"],
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
    "kube-prometheus-stack": [
        (("grafana", "enabled"), False, "Modelplane doesn't use Grafana dashboards"),
        (("alertmanager", "enabled"), False, "Modelplane doesn't route alerts"),
    ],
}

# Values Modelplane adds that AICR's recipes carry no opinion on,
# deep-set into the hydrated bundle after the merge. Unlike MANAGED
# these aren't --set (a structured value can't ride a --set flag) and
# need no assertion - the generator writes them itself. They keep the
# monitoring shape identical on every cloud: the collecting-engine-
# metrics guide's PodMonitors and the envoy proxy scrape config work
# the same on a generated cloud as on a hand-written one.
MODELPLANE_VALUES = {
    "kube-prometheus-stack": [
        (
            ("prometheus", "prometheusSpec", "podMonitorSelectorNilUsesHelmValues"),
            False,
            "discover user PodMonitors (the collecting-engine-metrics guide) beyond the release's own",
        ),
        (
            ("prometheus", "prometheusSpec", "podMonitorNamespaceSelector"),
            {},
            "discover PodMonitors across all namespaces",
        ),
        (
            ("prometheus", "prometheusSpec", "additionalScrapeConfigs"),
            [
                {
                    "job_name": "envoy-gateway-proxy",
                    "kubernetes_sd_configs": [
                        {
                            "role": "pod",
                            "namespaces": {
                                "names": ["envoy-gateway-system"],
                            },
                        },
                    ],
                    "relabel_configs": [
                        {
                            "source_labels": [
                                "__meta_kubernetes_pod_label_app_kubernetes_io_component",
                            ],
                            "action": "keep",
                            "regex": "proxy",
                        },
                        {
                            "source_labels": ["__address__"],
                            "action": "replace",
                            "regex": "([^:]+)(?::\\d+)?",
                            "replacement": "$1:19001",
                            "target_label": "__address__",
                        },
                    ],
                    "metrics_path": "/stats/prometheus",
                },
            ],
            "scrape envoy proxy pods for in-flight request metrics (envoy_cluster_upstream_rq_active)",
        ),
    ],
}

# Per-cloud expected values (and the reason) where a cloud
# legitimately differs from the global table.
MANAGED_CLOUD = {
    "gke": {
        ("nvidia-dra-driver-gpu", ("nvidiaDriverRoot",)): (
            "/home/kubernetes/bin/nvidia",
            "Google's installer puts the driver here under every gpuStack value",
        ),
        # Forcing the toolkit off here leaves every operand failing
        # pod-sandbox creation with 'no runtime for "nvidia" is
        # configured'; the node-image assumption behind the global
        # toolkit=false holds everywhere but on COS.
        ("gpu-operator", ("toolkit", "enabled")): (
            True,
            "COS ships no nvidia containerd runtime; per Google's and AICR's "
            "GKE guidance the operator's toolkit configures one",
        ),
    },
}

# Managed paths that exist only on one cloud, appended to that cloud's
# table (MANAGED_CLOUD can only override global entries). GKE auto-runs
# a managed DCGM exporter on GPU nodes (gke-managed-system), and DCGM's
# profiling module is single-client per GPU - a second exporter dies
# with 'the third-party Profiling module returned an unrecoverable
# error'. GPU telemetry on GKE is the platform's, like the driver.
MANAGED_CLOUD_EXTRA = {
    "gke": {
        "gpu-operator": [
            (("dcgm", "enabled"), False, "GKE's managed DCGM owns the GPU's single-client profiling module"),
            (("dcgmExporter", "enabled"), False, "GKE's managed dcgm-exporter provides GPU telemetry"),
        ],
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
    overrides = MANAGED_CLOUD.get(cloud, {})
    out = {}
    for component, entries in MANAGED.items():
        out[component] = [(path, *overrides.get((component, path), (value, why))) for path, value, why in entries]
    for component, entries in MANAGED_CLOUD_EXTRA.get(cloud, {}).items():
        out.setdefault(component, []).extend(entries)
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


def prune_nulls(values: Values, prefix: tuple[str, ...] = ()) -> list[str]:
    """Delete null-valued keys from hydrated values, returning their paths.

    A null in a recipe's values is a Helm tombstone: when Helm merges
    values files it deletes the key from a lower layer, and aicr's
    bundler preserves nulls expecting that path. The bundler already
    merged the layers a tombstone aimed at, so by the time values land
    here a null only leaks into the rendered manifests - harmless where
    a field is nullable, fatal where a CRD's schema forbids it (the
    Prometheus CRD rejects storage.emptyDir null). So every null goes,
    reported as a finding. One consequence: a tombstone aimed at a
    chart default lets that default come back; if that is ever wrong
    for a path, pin an explicit value in MODELPLANE_VALUES instead.
    """
    pruned = []
    for key in list(values):
        val = values[key]
        if val is None:
            del values[key]
            pruned.append(".".join((*prefix, key)))
        elif isinstance(val, dict):
            pruned += prune_nulls(val, (*prefix, key))
        elif isinstance(val, list):
            for i, item in enumerate(val):
                if isinstance(item, dict):
                    pruned += prune_nulls(item, (*prefix, f"{key}[{i}]"))
    return pruned


def scope_tolerations(name: str, values: Values) -> list[str]:
    """Rewrite the bundler's wildcard tolerations per TOLERATIONS.

    Mutates values in place and returns findings. A wildcard at a path
    the table doesn't classify fails the run - fail closed, like
    ALLOW/DROP.
    """
    table = TOLERATIONS.get(name, {})
    findings = []

    def walk(node: object, prefix: tuple[str, ...]) -> None:
        if isinstance(node, dict):
            for key, val in node.items():
                if tolerations_key(key) and val == WILDCARD_TOLERATION:
                    dotted = ".".join((*prefix, key))
                    if dotted not in table:
                        sys.exit(
                            f"wildcard toleration at unclassified path "
                            f"{name}.{dotted}: classify it in TOLERATIONS - fail closed"
                        )
                    scoped = table[dotted]
                    node[key] = [dict(t) for t in scoped]
                    findings.append(
                        f"scoped toleration: {name}.{dotted} -> "
                        + ("the GPU taint" if scoped else "none (system pool only)")
                    )
                else:
                    walk(val, (*prefix, key))
        elif isinstance(node, list):
            for i, item in enumerate(node):
                walk(item, (*prefix, f"[{i}]"))

    walk(values, ())
    return findings


def scope_manifest_tolerations(name: str, manifest: Values) -> list[str]:
    """scope_tolerations for a rendered manifest, keyed by kind and name."""
    key = (name, manifest.get("kind"), manifest.get("metadata", {}).get("name"))
    findings = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for k, val in node.items():
                if tolerations_key(k) and val == WILDCARD_TOLERATION:
                    if key not in MANIFEST_TOLERATIONS:
                        sys.exit(
                            f"wildcard toleration in manifest {key[1]}/{key[2]} of "
                            f"{name}: classify it in MANIFEST_TOLERATIONS - fail closed"
                        )
                    scoped = MANIFEST_TOLERATIONS[key]
                    node[k] = [dict(t) for t in scoped]
                    findings.append(
                        f"scoped toleration: {name} manifest {key[1]}/{key[2]} -> "
                        + ("the GPU taint" if scoped else "none (system pool only)")
                    )
                else:
                    walk(val)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(manifest)
    return findings


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
            f'against {AICR_PIN}: follow "Bumping aicr" in CONTRIBUTING.md '
            "(and update nix/aicr.nix) rather than mixing releases"
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
        key, dra_drivers = ALLOW[name]

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
        for path, value, why in MODELPLANE_VALUES.get(name, []):
            if lookup(values, path) not in (ABSENT, value):
                sys.exit(
                    f"modelplane value {name}.{'.'.join(path)} collides with a "
                    "recipe-carried value - reconcile the tables, fail closed"
                )
            set_path(values, path, value)
            findings.append(f"modelplane value: {name}.{'.'.join(path)} ({why})")
        for dotted in prune_nulls(values):
            findings.append(f"pruned null: {name}.{dotted} (a Helm tombstone; see prune_nulls)")
        findings += scope_tolerations(name, values)

        depends = []
        for dep in ref.get("dependencyRefs", []):
            if dep in ALLOW:
                depends.append(ALLOW[dep][0])
            elif dep in DROP:
                findings.append(
                    f"dropped dep: {name} -> {dep} (dropped; the join with "
                    "the hand-written files must satisfy it or nothing needed it)"
                )
            else:
                sys.exit(f"dependency {name} -> {dep!r} names an unclassified component - fail closed")

        namespace = NAMESPACE_PINS.get(key, ref["namespace"])
        if namespace != ref["namespace"]:
            findings.append(f"pinned namespace: {name}: {ref['namespace']} -> {namespace} (see NAMESPACE_PINS)")

        # The bundler ships pre-manifests at sync-wave N-1 - resources
        # the chart's own pods depend on, like the critical-pods
        # ResourceQuota it synthesizes for gkeCriticalPriority
        # components (GKE rejects system-node-critical pods in a
        # namespace without one; aicr#915). The chart depends on them,
        # so they exist before its pods and outlive it on teardown.
        pre = sorted(bundle_dir.glob(f"[0-9][0-9][0-9]-{name}-pre/templates/*.yaml"))
        if pre:
            # The chart's namespace leads the entry: aicr's helm
            # deployer installs the pre chart with --create-namespace,
            # and without the namespace the pre Objects can't apply
            # while the chart that would create it waits on them - a
            # deadlock the first GKE run hit.
            manifests = [
                {
                    "apiVersion": "v1",
                    "kind": "Namespace",
                    "metadata": {"name": namespace},
                },
            ]
            for f in pre:
                manifests.extend(d for d in yaml.safe_load_all(f.read_text()) if d)
            for doc in manifests:
                findings += scope_manifest_tolerations(name, doc)
            findings.append(
                f"pre-manifests: {name}: {len(manifests)} objects (namespace + {', '.join(f.name for f in pre)})"
            )
            pre_entry = {
                "type": "Manifests",
                "key": f"{key}-pre-manifests",
                "manifests": manifests,
            }
            # The chart's own tags, because the chart depends on this
            # entry - join()'s dependency-tagging rule.
            if dra_drivers:
                pre_entry["dra_drivers"] = dra_drivers
            components.append(pre_entry)
            depends.append(f"{key}-pre-manifests")

        entry = {
            "type": "Chart",
            "key": key,
            # provider-helm upgrades in place only while the release
            # name and namespace hold still; mp- reserves a namespace
            # (see the design and NAMESPACE_PINS).
            "release": f"mp-{ref['chart']}",
            "namespace": namespace,
            "chart": ref["chart"],
            "repository": ref["source"],
            "version": ref["version"],
        }
        if depends:
            entry["depends_on"] = depends
        if dra_drivers:
            entry["dra_drivers"] = dra_drivers
        if values:
            entry["values"] = values
        components.append(entry)

        # Components are not always just charts: on AKS the
        # gpu-operator carries a toolkit-hardening manifest. The bundle
        # materializes them under <NNN>-<name>-post/templates/, and
        # they render as provider-kubernetes Objects. Like the
        # pre-manifests above, the bundle is the source of truth, not
        # the recipe: the registry can supply default manifestFiles a
        # componentRef never declares. A manifest that disappears on a
        # bump shows up as a dropped key in the generated diff and the
        # golden inventory test.
        rendered = sorted(bundle_dir.glob(f"[0-9][0-9][0-9]-{name}-post/templates/*.yaml"))
        if rendered:
            manifests = []
            for f in rendered:
                manifests.extend(d for d in yaml.safe_load_all(f.read_text()) if d)
            for doc in manifests:
                findings += scope_manifest_tolerations(name, doc)
            findings.append(f"manifests: {name}: {len(manifests)} objects ({', '.join(f.name for f in rendered)})")
            post_entry = {
                "type": "Manifests",
                "key": f"{key}-manifests",
                "depends_on": [key],
                "manifests": manifests,
            }
            # Depending on a tagged chart requires carrying its tags, so
            # the filter keeps or drops the pair together.
            if dra_drivers:
                post_entry["dra_drivers"] = dra_drivers
            components.append(post_entry)

    # The bundle is consumed fail-closed, like the recipe: every
    # component directory must belong to a classified component and use
    # a layout this transform reads (main, -pre, -post). A new suffix or
    # an unclassified name means aicr grew a bundle shape we would
    # otherwise silently drop - the pre-manifests were exactly that
    # once (a GKE critical-pods quota discarded unread).
    for bundle_entry in sorted(bundle_dir.iterdir()):
        if not bundle_entry.is_dir() or not re.match(r"\d{3}-", bundle_entry.name):
            continue
        rest = bundle_entry.name[4:]
        for suffix in ("-pre", "-post"):
            if rest.endswith(suffix):
                rest = rest[: -len(suffix)]
                break
        if rest not in ALLOW and rest not in DROP:
            sys.exit(f"bundle directory {bundle_entry.name!r} belongs to no classified component - fail closed")

    # A chart another component depends on renders with helm --wait, so
    # its Ready means healthy and the function's install gate orders
    # dependents on health rather than deploy. Derived from the kept
    # edges, so the graph and the waits can't drift apart. (cert-manager
    # also picks this up from its in-recipe dependents; the cross-half
    # envoy-gateway edge needs it too.)
    depended_on = {dep for entry in components for dep in entry.get("depends_on", [])}
    for entry in components:
        if entry["type"] == "Chart" and entry["key"] in depended_on:
            entry["wait"] = True
            findings.append(f"wait: {entry['key']} (another component depends on it)")

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


def check_namespace_holds(cloud: str, components: list[Values]) -> None:
    """Fail the run when a component's namespace moves from the last generated file.

    A moved namespace is a wedged upgrade, not a reinstall: provider-helm
    finds no release at the new coordinates, its fresh install trips over
    the old release's ownership annotations on cluster-scoped objects,
    and the ServingStack stays not Ready on every existing cluster while
    CI - fresh clusters only - passes. Pin the old namespace in
    NAMESPACE_PINS, or land the move as a documented migration that
    deletes this check's trip wire deliberately.
    """
    path = OUT / f"{cloud}.py"
    if not path.exists():
        return
    previous = {}
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Call):
            kw = {k.arg: k.value.value for k in node.keywords if isinstance(k.value, ast.Constant)}
            if "key" in kw and "namespace" in kw:
                previous[kw["key"]] = kw["namespace"]
    for entry in components:
        was = previous.get(entry.get("key"))
        if was and was != entry.get("namespace"):
            sys.exit(
                f"{cloud}: {entry['key']} moved namespace {was!r} -> "
                f"{entry['namespace']!r}, which Helm can't do: existing "
                "clusters would wedge on a fresh-install ownership error "
                "(NVIDIA/aicr#2899). Pin the old namespace in NAMESPACE_PINS "
                "or land the move as a documented migration - fail closed"
            )


def pyfmt(value: object) -> str:
    """Render a value as a Python literal.

    Nearly repr, and layout is ruff format's job (the .#stacks app runs
    it over the output). The one thing ruff can't do is the reason this
    exists: a multi-line string must be emitted triple-quoted, because
    repr escapes its newlines into one enormous line that ruff will
    never split - and a per-line diff of a config blob (dcgm-exporter's
    metrics CSV) is what makes a bump reviewable.
    """
    if isinstance(value, dict):
        return "{" + ", ".join(f"{k!r}: {pyfmt(v)}" for k, v in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ", ".join(pyfmt(item) for item in value) + "]"
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
    fields = [
        "key",
        "release",
        "namespace",
        "chart",
        "repository",
        "version",
        "wait",
        "depends_on",
        "dra_drivers",
        "values",
        "manifests",
    ]
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
        "# DRA drivers this cloud supports that arrive outside the serving",
        "# stack; the stacks package folds them into its derived",
        "# supported-drivers table (see generate.py's EXTERNAL_DRIVERS).",
        f"EXTERNAL_DRIVERS: list[str] = {pyfmt(EXTERNAL_DRIVERS.get(cloud, []))}",
        "",
        "COMPONENTS: list[Component] = [",
    ]
    for entry in components:
        lines.append(f"    {entry['type']}(")
        for field in fields:
            if field not in entry:
                continue
            lines.append(f"        {field}={pyfmt(entry[field])},")
        lines.append("    ),")
    lines += ["]", ""]
    text = "\n".join(lines)
    ast.parse(text)  # a generated file that doesn't parse fails the build
    (OUT / f"{cloud}.py").write_text(text)


def exporter_csv(cloud: str, components: list[Values]) -> str:
    """The DCGM exporter metrics CSV in the cloud's gpu-operator values."""
    for entry in components:
        if entry.get("key") == "gpu-operator":
            data = lookup(entry["values"], ("dcgmExporter", "config", "data"))
            if isinstance(data, str):
                return data
    sys.exit(f"{cloud}: no dcgm-exporter metrics CSV in the gpu-operator values - fail closed")


def render_dcgm(csv: str) -> str:
    lines = [
        LICENSE_HEADER,
        "",
        "# Generated by generate.py. Do not edit.",
        "# Regenerate with `nix run .#stacks`.",
        "#",
        "# The DCGM exporter's metrics CSV, identical on every cloud AICR",
        "# covers - emit_dcgm fails the run when the clouds diverge. The",
        "# generated files beside this one carry the CSV inline and stay",
        "# self-contained; the hand-written clouds that run the same",
        "# exporter import it from here, so an aicr bump moves them in the",
        "# same review as the generated clouds.",
        "",
        f"DCGM_METRICS_CSV = {pyfmt(csv)}",
        "",
    ]
    text = "\n".join(lines)
    ast.parse(text)  # a generated file that doesn't parse fails the build
    return text


def emit_dcgm(csvs: dict[str, str]) -> None:
    """Write the shared metrics CSV module the hand-written clouds import."""
    if len(set(csvs.values())) > 1:
        sys.exit(
            f"dcgm-exporter metrics CSV differs between {', '.join(sorted(csvs))}; "
            "the hand-written clouds import one shared copy - decide which they follow"
        )
    text = render_dcgm(next(iter(csvs.values())))
    out = OUT / "dcgm.py"
    if set(csvs) != set(CLOUDS) and out.exists() and out.read_text() != text:
        sys.exit(
            f"the dcgm-exporter metrics CSV moved but only {', '.join(sorted(csvs))} "
            "regenerated; regenerate every cloud (`nix run .#stacks` with no "
            "arguments) so dcgm.py moves with all of them"
        )
    out.write_text(text)
    print(f"generated {out.relative_to(ROOT)}")


def regenerate(cloud: str, workdir: pathlib.Path, catalog: pathlib.Path) -> str:
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
    check_namespace_holds(cloud, components)
    floor, floor_findings = floors(cloud, spec, recipe, workdir, catalog)
    for finding in findings + floor_findings:
        print(f"[{cloud}] {finding}", file=sys.stderr)

    digest = hashlib.sha256(recipe_file.read_bytes()).hexdigest()
    emit(cloud, spec, components, recipe, digest, floor)
    return exporter_csv(cloud, components)


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
        csvs = {}
        for cloud in clouds or CLOUDS:
            csvs[cloud] = regenerate(cloud, workdir, catalog)
            print(f"generated {OUT.relative_to(ROOT)}/{cloud}.py")
        emit_dcgm(csvs)


if __name__ == "__main__":
    main(sys.argv[1:])

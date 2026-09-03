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

"""A cloud half resolving to AMD's DRA driver instead of NVIDIA's.

The file the design's vendor-neutrality goal promises: a non-NVIDIA
accelerator is a cloud whose source list is entirely hand-written, one
more value of the cloud axis, not a new mechanism. AICR is unlikely to
ever cover one - its accelerator values are all NVIDIA SKUs and it
fails closed on a cluster mixing vendors.

UNVALIDATED: no cluster type maps to this file yet (see __init__.py),
and the pins below have not run on hardware. AMD's DRA driver
(ROCm/k8s-gpu-dra-driver) publishes ResourceSlices under the
gpu.amd.com device class in place of NVIDIA's gpu.nvidia.com; wiring a
ModelReplica to request it is follow-up work.
"""

from function.stacks.components import Chart, Component

COMPONENTS: list[Component] = [
    Chart(
        key="node-feature-discovery",
        release="mp-node-feature-discovery",
        namespace="node-feature-discovery",
        chart="node-feature-discovery",
        repository="oci://registry.k8s.io/nfd/charts",
        version="0.18.3",
        # As on the NVIDIA clouds, the worker must tolerate the GPU
        # taint to label the nodes the DRA driver targets; AMD nodes
        # carry an amd.com/gpu taint from AMD's own operator ecosystem.
        values={
            "worker": {
                "tolerations": [
                    {
                        "key": "amd.com/gpu",
                        "operator": "Exists",
                        "effect": "NoSchedule",
                    },
                ],
            },
        },
    ),
    Chart(
        key="amd-gpu-dra-driver",
        release="mp-k8s-gpu-dra-driver",
        namespace="amd-gpu-dra-driver",
        chart="k8s-gpu-dra-driver",
        repository="https://rocm.github.io/k8s-gpu-dra-driver",
        version="v1.0.1",
    ),
]

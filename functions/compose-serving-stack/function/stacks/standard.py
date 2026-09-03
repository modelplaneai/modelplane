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

"""Hand-written components only the Standard stack installs.

LeaderWorkerSet gang-deploys a multi-node engine; the Dynamo stack
replaces it with Grove and friends (see dynamo.py). The pin moves to
AICR's on the covered clouds once NVIDIA/aicr#2500 (an lws component)
merges and releases.
"""

from function.stacks.components import Chart, Component

COMPONENTS: list[Component] = [
    Chart(
        key="leader-worker-set",
        release="mp-lws",
        namespace="lws-system",
        chart="lws",
        repository="oci://registry.k8s.io/lws/charts",
        version="v0.8.0",
    ),
]

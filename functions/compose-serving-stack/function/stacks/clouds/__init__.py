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

"""The cloud halves of the serving stack.

A cloud half comes from one of two places: generated/ below, where a
build-time generator covers the cloud (one subdirectory per tool), or
a hand-written file at the top of this package where none does. Both
have the same shape, so which clouds are generated can change without
changing anything else.
"""

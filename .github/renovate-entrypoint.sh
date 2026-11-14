#!/bin/bash
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

# Entrypoint for the self-hosted Renovate container (see
# workflows/renovate.yml). Installs Nix so postUpgradeTasks can run
# `modelplane-nix run .#build`, which regenerates the committed Pydantic
# models under schemas/python/ after a dependency bump in
# crossplane-project.yaml.

set -e

# Our flake needs Nix 2.19 or later (for builtins.convertHash), which rules out
# Ubuntu's nix-bin package. Install a static Nix build with the Renovate
# image's containerbase installer instead.
NIX_VERSION=2.35.6
NIX_BIN=/opt/containerbase/tools/nix/${NIX_VERSION}/bin/nix

echo "Installing Nix..."
install-tool nix "${NIX_VERSION}"

mkdir -p /etc/nix
cat >/etc/nix/nix.conf <<'EOF'
# Enable flakes and the nix command (e.g. nix run, nix build).
experimental-features = nix-command flakes

# Run builds as the calling user, not dedicated nixbld users. This avoids
# needing to create the nixbld group and users in this ephemeral container.
build-users-group =

# Build derivations in parallel, one per CPU core.
max-jobs = auto
EOF

# The nix on PATH is containerbase's wrapper, which Renovate also uses when it
# updates flake.lock. It ignores the config above and keeps its store under
# /tmp, where nothing can be substituted from cache.nixos.org. So all nix
# commands we run (e.g. postUpgradeTasks) go through this launcher, which runs
# the binary directly with the config above and the default /nix/store.
cat >/usr/local/bin/modelplane-nix <<EOF
#!/bin/bash
exec env NIX_CONF_DIR=/etc/nix ${NIX_BIN} "\$@"
EOF
chmod +x /usr/local/bin/modelplane-nix

modelplane-nix --version

renovate

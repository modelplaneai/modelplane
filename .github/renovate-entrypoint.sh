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

echo "Installing Nix..."
apt-get update && apt-get install -y nix-bin

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

# Renovate installs its own Nix when it updates flake.lock. It goes earlier on
# PATH than ours and ignores the config above, so all nix commands we run (e.g.
# postUpgradeTasks) will go through this launcher, which pins both the binary
# and the config it reads.
cat >/usr/local/bin/modelplane-nix <<'EOF'
#!/bin/bash
exec env NIX_CONF_DIR=/etc/nix /usr/bin/nix "$@"
EOF
chmod +x /usr/local/bin/modelplane-nix

echo "Nix $(modelplane-nix --version) installed successfully"

renovate

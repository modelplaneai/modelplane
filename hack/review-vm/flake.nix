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

# The Firecracker microVM the review workflow runs the review agent in. It's a
# flake of its own so the project's flake doesn't depend on microvm.nix.
#
# The VM shares nothing with the runner. Its job arrives as a tar on a
# read-only disk: the PR's checkout, the PR's context and the agent's prompt.
# It writes the review and the agent's transcript as a tar to a second disk,
# which the runner reads without mounting. Its only network is a tap device the
# runner firewalls, which reaches the internet and the model proxy at
# 192.168.100.1.
{
  description = "The microVM the review agent runs in";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";
    # opencode, Vale and vale-ls come from unstable. 26.05 ships an older major
    # version of opencode, and a Vale that ignores the relative StylesPath in
    # the repo's Vale config.
    nixpkgs-unstable.url = "github:NixOS/nixpkgs/1c89083e451e891152365b799595cf7790a43325";
    microvm = {
      url = "github:microvm-nix/microvm.nix/b7bd556974fa8a171b5b639200e81d52244dfde4";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    {
      nixpkgs,
      nixpkgs-unstable,
      microvm,
      ...
    }:
    {
      nixosConfigurations.agent = nixpkgs.lib.nixosSystem {
        system = "x86_64-linux";
        modules = [
          microvm.nixosModules.microvm
          (
            { pkgs, lib, ... }:
            let
              unstable = nixpkgs-unstable.legacyPackages.${pkgs.stdenv.hostPlatform.system};

              # opencode 2 has no language server support of its own. This
              # plugin adds it, so the agent sees Vale's diagnostics on every
              # review file it writes.
              lspPlugin = pkgs.fetchFromGitHub {
                owner = "christensenjairus";
                repo = "opencode-v2-lsp-support";
                rev = "4a17bc8379e572ab104ec26b55f92ee9b51dce37";
                hash = "sha256-vSlq1GVtqEdqYBVZU3+UyCH5Ke4nQwY1Y9VWcoUWiu8=";
              };

              opencodeConfig = builtins.toJSON {
                # The review script adds the model and its provider, which the
                # workflow passes in model.json.
                autoupdate = false;
                plugins = [ "${lspPlugin}" ];
                lsp.vale = {
                  command = [ "vale-ls" ];
                  extensions = [ ".md" ];
                  initialization = {
                    configPath = "/work/pr/.vale.ini";
                    installVale = false;
                    syncOnStartup = false;
                  };
                };
              };
            in
            {
              system.stateVersion = "26.05";

              microvm = {
                hypervisor = "firecracker";
                # Firecracker needs one or an even number of vCPUs with SMT.
                vcpu = 4;
                mem = 12288;

                interfaces = [
                  {
                    type = "tap";
                    id = "vm0";
                    mac = "02:00:00:00:00:01";
                  }
                ];

                # Firecracker attaches the store disk first, then these in
                # order, so they're vdb through vdf. The runner creates them
                # in the working directory it starts the VM from.
                writableStoreOverlay = "/nix/.rw-store";
                volumes = [
                  {
                    image = "store.img";
                    mountPoint = "/nix/.rw-store";
                    size = 40960;
                  }
                  {
                    image = "docker.img";
                    mountPoint = "/var/lib/docker";
                    size = 20480;
                  }
                  {
                    image = "work.img";
                    mountPoint = "/work";
                    size = 20480;
                  }
                  {
                    image = "in.tar";
                    mountPoint = null;
                    autoCreate = false;
                    readOnly = true;
                  }
                  {
                    image = "out.tar";
                    mountPoint = null;
                    autoCreate = false;
                  }
                ];
              };

              networking = {
                hostName = "agent";
                useNetworkd = true;
                useDHCP = false;
                enableIPv6 = false;
              };
              systemd.network.networks."10-vm0" = {
                matchConfig.MACAddress = "02:00:00:00:00:01";
                address = [ "192.168.100.2/30" ];
                routes = [ { Gateway = "192.168.100.1"; } ];
                dns = [
                  "1.1.1.1"
                  "8.8.8.8"
                ];
              };

              nix.settings.experimental-features = [
                "nix-command"
                "flakes"
              ];
              virtualisation.docker.enable = true;

              users.users.root.password = "";
              services.getty.autologinUser = "root";

              # Unpack the job, run the agent, write its output back as a tar,
              # then reboot. Firecracker has no power-off device, so a halted
              # guest leaves it running. It exits on a guest reboot.
              systemd.services.review = {
                wantedBy = [ "multi-user.target" ];
                after = [
                  "network-online.target"
                  "docker.service"
                ];
                wants = [ "network-online.target" ];
                path = [
                  unstable.opencode
                  pkgs.bash
                  pkgs.coreutils
                  pkgs.curl
                  pkgs.docker
                  pkgs.findutils
                  pkgs.gh
                  pkgs.git
                  pkgs.gnugrep
                  pkgs.gnutar
                  pkgs.jq
                  pkgs.kind
                  pkgs.kubectl
                  pkgs.nix
                  pkgs.procps
                  pkgs.ripgrep
                  unstable.vale
                  unstable.vale-ls
                ];
                environment = {
                  HOME = "/root";
                  TMPDIR = "/work/tmp";
                  OPENCODE_CONFIG_CONTENT = opencodeConfig;
                };
                serviceConfig = {
                  Type = "oneshot";
                  StandardOutput = "journal+console";
                  StandardError = "journal+console";
                };
                script = ''
                  set +e
                  mkdir -p /work/tmp /work/out
                  tar -xf /dev/vde -C /work
                  # A token that can only read this repo's contents, PRs and
                  # issues, all of which are public, so the agent can use gh.
                  GH_TOKEN=$(cat /work/gh-token)
                  export GH_TOKEN
                  rm /work/gh-token
                  cd /work/pr
                  git config --global --add safe.directory /work/pr
                  mkdir -p _output/review
                  vale sync
                  # The console, which the workflow's log shows, gets opencode's
                  # own readable output. The transcript is each session's
                  # export, the agent's and every subagent's, with their costs.
                  # The agent reaches the model through the runner's proxy, which
                  # replaces the API key.
                  model=$(jq -r .model /work/model.json)
                  case $(jq -r .api /work/model.json) in
                    openai)
                      config=$(jq -n --arg model "$model" '{
                        model: ("inference/" + $model),
                        provider: {inference: {
                          npm: "@ai-sdk/openai-compatible",
                          options: {baseURL: "http://192.168.100.1:8787/v1", apiKey: "unused"},
                          models: {($model): {}}
                        }}
                      }')
                      ;;
                    *)
                      config=$(jq -n --arg model "$model" '{
                        model: ("anthropic/" + $model),
                        provider: {anthropic: {
                          options: {baseURL: "http://192.168.100.1:8787/v1", apiKey: "unused"}
                        }}
                      }')
                      ;;
                  esac
                  OPENCODE_CONFIG_CONTENT=$(jq -c --argjson config "$config" '. + $config' <<< "$OPENCODE_CONFIG_CONTENT")
                  export OPENCODE_CONFIG_CONTENT
                  opencode run --standalone --auto --agent review "$(cat /work/prompt)"
                  status=$?
                  mkdir -p /work/out/transcript
                  pending=$(opencode session list --standalone --format json | jq -r '.[].id')
                  while [ -n "$pending" ]; do
                    next=""
                    for id in $pending; do
                      out=/work/out/transcript/$id.json
                      [ -e "$out" ] && continue
                      opencode session export --standalone "$id" > "$out"
                      next="$next $(grep -o 'subagent sessionID=\\"ses_[A-Za-z0-9]*' "$out" | grep -o 'ses_[A-Za-z0-9]*' | sort -u)"
                    done
                    pending=$next
                  done
                  # opencode run exits 1 when any model request fails, even one
                  # it retried and recovered from. The root session's outcome
                  # says whether the review finished.
                  outcome=$(jq -r 'select(.info.parentID == null) | .info.outcome' /work/out/transcript/*.json)
                  if [ "$outcome" = succeeded ]; then status=0; fi
                  echo "$status" > /work/out/exit
                  cp -r _output/review /work/out/review
                  tar -cf /dev/vdf -C /work/out .
                  sync
                  ${lib.getExe' pkgs.systemd "systemctl"} reboot
                '';
              };
            }
          )
        ];
      };
    };
}

# Interactive development commands for Modelplane.
#
# Apps run outside the Nix sandbox with full filesystem and network access.
# They're designed for local development.
#
# All apps are builder functions that take an attrset of arguments and return a
# complete app definition ({ type, meta.description, program }). Most use
# writeShellApplication to create the program. The text block is preprocessed:
#
#   ${somePkg}/bin/foo   -> /nix/store/.../bin/foo  (Nix store path)
#   ''${SOME_VAR}        -> ${SOME_VAR}             (shell variable, escaped)
#
# Each app declares its tool dependencies via runtimeInputs, with inheritPath
# set to false. This ensures apps only use explicitly declared tools.
{ pkgs }:
{
  # Load the web UI image into Docker.
  loadImage =
    { image }:
    {
      type = "app";
      meta.description = "Load the web UI container image into Docker";
      program = pkgs.lib.getExe (
        pkgs.writeShellApplication {
          name = "modelplane-load-image";
          runtimeInputs = [ pkgs.docker-client ];
          inheritPath = false;
          text = ''
            docker load < ${image}
            echo "Loaded modelplane-ui:latest"
          '';
        }
      );
    };

  # Lint Go code.
  lint = _: {
    type = "app";
    meta.description = "Lint Go code";
    program = pkgs.lib.getExe (
      pkgs.writeShellApplication {
        name = "modelplane-lint";
        runtimeInputs = [ pkgs.golangci-lint ];
        inheritPath = false;
        text = ''
          cd ui
          golangci-lint run ./...
        '';
      }
    );
  };

  # Build the Crossplane project (XRDs, functions, and compositions).
  buildCrossplane =
    { up, dockerCredentialUp }:
    {
      type = "app";
      meta.description = "Build the Crossplane project";
      program = pkgs.lib.getExe (
        pkgs.writeShellApplication {
          name = "modelplane-build-crossplane";
          runtimeInputs = [
            up
            dockerCredentialUp
          ];
          inheritPath = false;
          text = ''
            up project build "$@"
          '';
        }
      );
    };

  # Build the Crossplane project and run composition tests. These need Docker
  # for the function-python runtime, so they can't run in the Nix sandbox.
  testCrossplane =
    { up, dockerCredentialUp }:
    {
      type = "app";
      meta.description = "Build the Crossplane project and run composition tests";
      program = pkgs.lib.getExe (
        pkgs.writeShellApplication {
          name = "modelplane-test-crossplane";
          runtimeInputs = [
            up
            dockerCredentialUp
          ];
          inheritPath = false;
          text = ''
            up project build
            echo ""
            echo "Running composition tests..."
            up test run tests/*
          '';
        }
      );
    };

  # Push the Crossplane project to a registry.
  #
  # Auto-generates a dev version tag from git metadata:
  #   v0.1.0-dev.<commit-count>.g<short-hash>
  #
  # Pass --tag to override, e.g.:
  #   nix run .#push-crossplane -- --tag v0.1.0
  pushCrossplane =
    { up, dockerCredentialUp }:
    {
      type = "app";
      meta.description = "Push the Crossplane project to a registry";
      program = pkgs.lib.getExe (
        pkgs.writeShellApplication {
          name = "modelplane-push-crossplane";
          runtimeInputs = [
            up
            dockerCredentialUp
            pkgs.git
          ];
          inheritPath = false;
          text = ''
            # Auto-generate a dev tag from git metadata unless --tag is
            # passed explicitly.
            if [[ ! " $* " =~ " --tag " ]]; then
              count=$(git rev-list --count HEAD)
              hash=$(git rev-parse --short HEAD)
              tag="v0.1.0-dev.''${count}.g''${hash}"
              echo "Pushing with tag: $tag"
              set -- --tag "$tag" "$@"
            fi
            up project push "$@"
          '';
        }
      );
    };

  # Run the web UI Go proxy for development.
  devProxy = _: {
    type = "app";
    meta.description = "Run the web UI proxy for development";
    program = pkgs.lib.getExe (
      pkgs.writeShellApplication {
        name = "modelplane-dev-proxy";
        runtimeInputs = [ pkgs.go ];
        inheritPath = false;
        text = ''
          export CGO_ENABLED=0
          cd ui
          go run ./cmd/proxy "$@"
        '';
      }
    );
  };

  # Run the web UI frontend Vite dev server. Node tooling (npm, vite) shells
  # out to sh and uses common coreutils.
  devFrontend = _: {
    type = "app";
    meta.description = "Run the web UI frontend Vite dev server";
    program = pkgs.lib.getExe (
      pkgs.writeShellApplication {
        name = "modelplane-dev-frontend";
        runtimeInputs = [
          pkgs.nodejs
          pkgs.bash
          pkgs.coreutils
        ];
        inheritPath = false;
        text = ''
          cd ui/frontend
          npm install --silent
          npx vite
        '';
      }
    );
  };
}

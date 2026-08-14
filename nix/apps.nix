# Development commands (nix run .#<app>).
#
# Apps run outside the Nix sandbox with full filesystem and network access.
# Each app declares its tool dependencies via runtimeInputs with inheritPath
# set to false, ensuring apps only use explicitly declared tools.
{ pkgs }:
{
  # Auto-fix linting and formatting issues across all languages.
  fix = _: {
    type = "app";
    meta.description = "Auto-fix lint and formatting issues";
    program = pkgs.lib.getExe (
      pkgs.writeShellApplication {
        name = "modelplane-fix";
        runtimeInputs = [
          pkgs.findutils
          pkgs.unstable.ruff
          pkgs.statix
          pkgs.deadnix
          pkgs.nixfmt
          pkgs.shellcheck
          pkgs.shfmt
          pkgs.gnupatch
          pkgs.addlicense
          pkgs.unstable.uv
        ];
        inheritPath = false;
        text = ''
          echo "Adding missing license headers..."
          addlicense -l apache -c "The Modelplane Authors." \
            -ignore '**/*.toml' \
            -ignore '**/*.yaml' \
            -ignore '**/*.yml' \
            functions/ docs/utils/validate/ nix.sh docs/vercel-build.sh

          echo "Formatting and linting Nix..."
          statix fix .
          deadnix --edit flake.nix nix/*.nix
          nixfmt flake.nix nix/*.nix

          echo "Formatting and linting shell..."
          find . -name '*.sh' -type f | while read -r script; do
            shellcheck --format=diff "$script" | patch -p1 || true
            shfmt -w "$script"
          done
          find . -name '*.sh' -type f -exec shellcheck {} +

          echo "Formatting and linting Python..."
          ruff format functions/
          ruff check --fix functions/

          echo "Refreshing uv.lock..."
          uv lock
        '';
      }
    );
  };

  # Build the Crossplane project. Materialises the Nix-built function runtime
  # images into _output/functions/ before invoking the CLI, which loads them
  # via the Tarball function source in crossplane-project.yaml.
  #
  # This is also the schema generation entrypoint. crossplane project build
  # generates the Pydantic models under schemas/python/ from both the XRDs in
  # apis/ and the project's dependency CRDs, and writes schemas/.lock.json.
  # (crossplane dependency update-cache, which the build calls internally, only
  # regenerates the dependency half; the XRD-derived models are written by the
  # build itself.)
  #
  # Schema generation is additive: it overwrites the files it generates but
  # never removes models or lock entries for XRDs or dependencies that have been
  # dropped or renamed. We delete schemas/ first so the result reflects only the
  # current XRDs and dependencies. Everything under schemas/ is generated (the
  # per-language bindings and the language-agnostic .lock.json), so it's safe to
  # remove wholesale and let the build recreate it.
  #
  # docker-credential-up remains available for resolving any dependencies that
  # require registry authentication.
  build =
    {
      crossplane,
      dockerCredentialUp,
      functionsPkg,
    }:
    {
      type = "app";
      meta.description = "Build the Crossplane project and regenerate schemas";
      program = pkgs.lib.getExe (
        pkgs.writeShellApplication {
          name = "modelplane-build";
          runtimeInputs = [
            crossplane
            dockerCredentialUp
            pkgs.coreutils
          ];
          inheritPath = false;
          text = ''
            mkdir -p _output
            rm -f _output/functions
            ln -s ${functionsPkg} _output/functions

            rm -rf schemas
            crossplane project build "$@"
          '';
        }
      );
    };

  # Build the project and run it in a local dev control plane (a KIND cluster
  # with its own OCI registry, managed by `crossplane project run`). This is
  # the fast local iteration loop: no real registry push - the CLI sideloads
  # packages into the local registry itself.
  run =
    {
      crossplane,
      dockerCredentialUp,
      functionsPkg,
    }:
    {
      type = "app";
      meta.description = "Build and run the project in a local dev control plane";
      program = pkgs.lib.getExe (
        pkgs.writeShellApplication {
          name = "modelplane-run";
          runtimeInputs = [
            crossplane
            dockerCredentialUp
            pkgs.coreutils
            pkgs.kind
            pkgs.kubectl
            pkgs.docker-client
            pkgs.nix
          ];
          inheritPath = false;
          text = ''
            mkdir -p _output
            rm -f _output/functions
            ln -s ${functionsPkg} _output/functions

            # The CLI's default --timeout of 5m covers waiting for the
            # installed configuration to become healthy, which first-time
            # image pulls through nix.sh's Docker-in-Docker daemon regularly
            # exceed. Default to a longer wait; an explicit --timeout on the
            # command line still wins.
            timeout_args=(--timeout 15m)
            for arg in "$@"; do
              case "$arg" in
                --timeout | --timeout=*) timeout_args=() ;;
              esac
            done

            # On failure, dump the package revision state: installs time out
            # with only "context deadline exceeded", and under nix.sh the
            # cluster is gone by the time anyone can look at it.
            if ! crossplane project run "''${timeout_args[@]}" "$@"; then
              echo ""
              echo "crossplane project run failed; package revision state:"
              for cluster in $(kind get clusters 2>/dev/null); do
                kind export kubeconfig --name "$cluster" >/dev/null 2>&1 || true
              done
              kubectl get pkgrev || true
              kubectl get pkgrev -o yaml || true
              failed=1
            else
              # The control plane is up and healthy - finish the setup the
              # getting-started flow otherwise does by hand. prerequisites.yaml
              # carries the modelplane-system namespace, composition RBAC, and
              # provider-helm's DeploymentRuntimeConfig and ImageConfig.
              kubectl apply -f docs/manifests/getting-started/prerequisites.yaml

              # crossplane project run installs providers before
              # prerequisites.yaml is applied, and Crossplane resolves
              # ImageConfig runtime configs only at ProviderRevision creation -
              # so provider-helm always comes up on the default runtime config,
              # whose ServiceAccount lacks the RBAC granted above. Point it at
              # its DeploymentRuntimeConfig explicitly.
              kubectl patch provider.pkg.crossplane.io upbound-provider-helm --type merge \
                -p '{"spec":{"runtimeConfigRef":{"apiVersion":"pkg.crossplane.io/v1beta1","kind":"DeploymentRuntimeConfig","name":"provider-helm-modelplane"}}}'
            fi

            # When running via nix.sh, the cluster lives inside the container's
            # Docker daemon and would vanish when the container exits. Drop into
            # a dev shell so the user can interact with it before exiting -
            # after a failure, that's where to debug the cluster.
            if [ "''${NIX_SH_CONTAINER:-}" = "1" ]; then
              echo ""
              echo "Entering development shell (exit to stop)..."
              exec nix develop
            fi

            exit "''${failed:-0}"
          '';
        }
      );
    };

  # Push the Crossplane project to a registry, then append the package's
  # Upbound Marketplace extensions to the pushed image. Uses a dev version tag
  # unless --tag is passed, e.g.: nix run .#push -- --tag v0.1.0
  #
  # The Marketplace renders package assets from a well-known extensions image
  # layout: icons/ (committed under extensions/), readme/readme.md (copied
  # from README.md), and release-notes/ (included when a release_notes.md
  # exists in the working directory - CI writes it on release runs).
  push =
    {
      crossplane,
      upbound,
      version,
    }:
    {
      type = "app";
      meta.description = "Push the Crossplane project to a registry";
      program = pkgs.lib.getExe (
        pkgs.writeShellApplication {
          name = "modelplane-push";
          # upbound provides both docker-credential-up, which the Crossplane
          # CLI uses to authenticate the push, and up, which appends the
          # extensions.
          runtimeInputs = [
            crossplane
            upbound
            pkgs.coreutils
            pkgs.yq-go
          ];
          inheritPath = false;
          text = ''
            if [[ ! " $* " =~ " --tag " ]]; then
              echo "Pushing with tag: ${version}"
              set -- --tag "${version}" "$@"
            fi
            crossplane project push "$@"

            tag=""
            while [ $# -gt 1 ]; do
              if [ "$1" = "--tag" ]; then tag="$2"; fi
              shift
            done
            repository=$(yq '.spec.repository' crossplane-project.yaml)

            extensions=$(mktemp -d)
            trap 'rm -rf "$extensions"' EXIT
            cp -R extensions/. "$extensions/"
            mkdir -p "$extensions/readme"
            cp README.md "$extensions/readme/readme.md"
            if [ -f release_notes.md ]; then
              mkdir -p "$extensions/release-notes"
              cp release_notes.md "$extensions/release-notes/"
            fi

            echo "Appending Marketplace extensions to $repository:$tag"
            up alpha xpkg append "$repository:$tag" --extensions-root "$extensions"
          '';
        }
      );
    };

  # Tear down the local dev control plane created by `nix run .#run`, removing
  # its KIND cluster and OCI registry.
  stop =
    { crossplane }:
    {
      type = "app";
      meta.description = "Tear down the local dev control plane";
      program = pkgs.lib.getExe (
        pkgs.writeShellApplication {
          name = "modelplane-stop";
          runtimeInputs = [
            crossplane
            pkgs.kind
            pkgs.docker-client
          ];
          inheritPath = false;
          text = ''
            crossplane project stop "$@"
          '';
        }
      );
    };

  # Run the two-cluster local end-to-end test: a workload
  # kind cluster registered via source: Existing (serving stack + model) and a
  # control-plane cluster (crossplane + the InferenceGateway). Two clusters
  # because the control-plane and workload layers both install the Gateway API
  # CRDs and collide on a single cluster. See e2e. Tear down with
  # `nix run .#e2e -- --clean`. This app just materialises the Nix-built
  # function images (as `run` does), then hands off to run.sh, which needs real
  # orchestration (a second cluster, a cross-cluster kubeconfig) that
  # `crossplane project run` flags can't express — kept a normal shell file so
  # it stays shellcheck-clean rather than escaped nix strings.
  e2e =
    {
      crossplane,
      functionsPkg,
    }:
    {
      type = "app";
      meta.description = "Run the local two-cluster end-to-end test";
      program = pkgs.lib.getExe (
        pkgs.writeShellApplication {
          name = "modelplane-e2e";
          runtimeInputs = [
            crossplane
            pkgs.coreutils
            pkgs.gnused
            pkgs.gnugrep
            pkgs.gawk
            pkgs.kind
            pkgs.kubectl
            pkgs.curl
            pkgs.docker-client
            pkgs.git
            pkgs.bash
          ];
          inheritPath = false;
          text = ''
            mkdir -p _output
            rm -f _output/functions
            ln -s ${functionsPkg} _output/functions

            exec bash e2e/run.sh "$@"
          '';
        }
      );
    };

  # Serve the docs site locally with live reload. Extra args pass through to
  # hugo server, e.g.: nix run .#docs-serve -- --port 8080
  docsServe = _: {
    type = "app";
    meta.description = "Serve the docs site locally with live reload";
    program = pkgs.lib.getExe (
      pkgs.writeShellApplication {
        name = "modelplane-docs-serve";
        # Hugo reads git metadata for last-modified dates (enableGitInfo).
        runtimeInputs = [
          pkgs.hugo
          pkgs.git
        ];
        inheritPath = false;
        text = ''
          hugo server --source docs "$@"
        '';
      }
    );
  };

  # Rebuild the docs site's JavaScript bundle. webpack writes the bundle into
  # the geekboot theme's assets, which are committed to git; rerun this and
  # commit the result after changing anything under docs/utils/webpack/src.
  docsGenerate = _: {
    type = "app";
    meta.description = "Rebuild the docs site JavaScript bundle";
    program = pkgs.lib.getExe (
      pkgs.writeShellApplication {
        name = "modelplane-docs-generate";
        # npm run spawns scripts via sh, so bash must be on PATH alongside node.
        runtimeInputs = [
          pkgs.nodejs
          pkgs.bash
        ];
        inheritPath = false;
        text = ''
          cd docs/utils/webpack
          npm ci
          npm run prod
          echo "Done. Review changes with 'git diff docs/themes/geekboot/assets/js'."
        '';
      }
    );
  };
}

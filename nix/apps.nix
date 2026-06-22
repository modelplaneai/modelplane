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
          pkgs.nixfmt-rfc-style
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
          ];
          inheritPath = false;
          text = ''
            mkdir -p _output
            rm -f _output/functions
            ln -s ${functionsPkg} _output/functions

            crossplane project run "$@"
          '';
        }
      );
    };

  # Push the Crossplane project to a registry. Uses a dev version tag unless
  # --tag is passed, e.g.: nix run .#push -- --tag v0.1.0
  push =
    {
      crossplane,
      dockerCredentialUp,
      version,
    }:
    {
      type = "app";
      meta.description = "Push the Crossplane project to a registry";
      program = pkgs.lib.getExe (
        pkgs.writeShellApplication {
          name = "modelplane-push";
          runtimeInputs = [
            crossplane
            dockerCredentialUp
          ];
          inheritPath = false;
          text = ''
            if [[ ! " $* " =~ " --tag " ]]; then
              echo "Pushing with tag: ${version}"
              set -- --tag "${version}" "$@"
            fi
            crossplane project push "$@"
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

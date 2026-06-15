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
          pkgs.ruff
          pkgs.statix
          pkgs.deadnix
          pkgs.nixfmt-rfc-style
          pkgs.shellcheck
          pkgs.shfmt
          pkgs.gnupatch
          pkgs.unstable.uv
        ];
        inheritPath = false;
        text = ''
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

  # Regenerate schemas from XRDs and dependencies. The Crossplane CLI writes
  # language bindings to schemas/; only schemas/python/ is committed to git.
  generate =
    { crossplane, pkgs }:
    {
      type = "app";
      meta.description = "Regenerate schemas from XRDs and dependencies";
      program = pkgs.lib.getExe (
        pkgs.writeShellApplication {
          name = "modelplane-generate";
          runtimeInputs = [
            crossplane
            pkgs.upbound
            pkgs.findutils
          ];
          inheritPath = false;
          text = ''
            crossplane dependency update-cache
            find schemas/python/models -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
            echo "Done. Review changes with 'git diff schemas/python/'."
          '';
        }
      );
    };

  # Build the Crossplane project. On Linux, materialises Nix-built function
  # runtime images into _output/functions/ before invoking the CLI. The CLI
  # loads them via the Tarball function source in crossplane-project.yaml.
  #
  # docker-credential-up is needed because `crossplane project build` calls
  # `crossplane dependency update-cache` to resolve providers and CRDs from
  # xpkg.upbound.io, which requires authentication.
  buildCrossplane =
    {
      crossplane,
      dockerCredentialUp,
      functionsPkg,
    }:
    {
      type = "app";
      meta.description = "Build the Crossplane project";
      program = pkgs.lib.getExe (
        pkgs.writeShellApplication {
          name = "modelplane-build-crossplane";
          runtimeInputs = [
            crossplane
            dockerCredentialUp
            pkgs.coreutils
          ];
          inheritPath = false;
          text =
            (
              if functionsPkg != null then
                ''
                  mkdir -p _output
                  rm -f _output/functions
                  ln -s ${functionsPkg} _output/functions
                ''
              else
                ''
                  echo "Note: function image builds are only supported on Linux." >&2
                ''
            )
            + ''
              crossplane project build "$@"
            '';
        }
      );
    };

  # Push the Crossplane project to a registry. Uses a dev version tag unless
  # --tag is passed, e.g.: nix run .#push-crossplane -- --tag v0.1.0
  pushCrossplane =
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
          name = "modelplane-push-crossplane";
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

  # Lint docs prose with Vale. Downloads configured style packages on first run
  # (requires network). Run from the repo root.
  docsVale = _: {
    type = "app";
    meta.description = "Lint docs prose with Vale";
    program = pkgs.lib.getExe (
      pkgs.writeShellApplication {
        name = "modelplane-docs-vale";
        runtimeInputs = [ pkgs.vale pkgs.findutils ];
        inheritPath = false;
        text = ''
          vale sync --config="$PWD/docs/utils/vale/.vale.ini"
          find "$PWD/docs/content" -name '*.md' -print0 | \
            xargs -0 vale --config="$PWD/docs/utils/vale/.vale.ini" "$@"
        '';
      }
    );
  };

  # Check internal links by building the Hugo site and running htmltest.
  docsHtmltest = _: {
    type = "app";
    meta.description = "Check docs internal links with htmltest";
    program = pkgs.lib.getExe (
      pkgs.writeShellApplication {
        name = "modelplane-docs-htmltest";
        runtimeInputs = [
          pkgs.hugo
          pkgs.htmltest
          pkgs.git
          pkgs.nodejs
        ];
        inheritPath = false;
        text = ''
          npm --prefix docs ci --silent
          hugo --source docs --minify
          htmltest --conf docs/utils/htmltest/.htmltest.yml "$@"
        '';
      }
    );
  };
}

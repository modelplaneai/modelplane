# The documentation site (https://modelplane.ai).
#
# The site is a Hugo project under docs/. Two asset pipelines feed it:
#
#   - JavaScript is bundled by webpack and committed to git (see the
#     docs-generate app), so the Hugo build needs no Node step for it.
#
#   - CSS is compiled from SCSS by Hugo, then run through PostCSS to prune
#     unused Bootstrap rules (PurgeCSS), sort media queries, and minify
#     (LightningCSS). Hugo shells out to the `postcss` CLI, so the build needs
#     a node_modules tree on disk. We build it reproducibly from
#     docs/package-lock.json with fetchNpmDeps, so the Hugo build stays inside
#     the Nix sandbox with no network.
{ pkgs, self }:
let
  # node_modules for the PostCSS pipeline, built from the committed lockfile.
  # Update fetchNpmDeps.hash below whenever docs/package-lock.json changes:
  #   nix run nixpkgs#prefetch-npm-deps -- docs/package-lock.json
  nodeModules = pkgs.stdenv.mkDerivation {
    pname = "modelplane-docs-node-modules";
    version = "0";
    src = ./../docs;

    nativeBuildInputs = [
      pkgs.nodejs
      pkgs.npmHooks.npmConfigHook
    ];

    npmDeps = pkgs.fetchNpmDeps {
      src = ./../docs;
      hash = "sha256-6JYtwuyq0TevYPHag2RI3dTh9eE7ZdO+mLLHL0sQ2+o=";
    };

    dontBuild = true;

    # cp -a copies node_modules verbatim, preserving any symlinks (e.g. under
    # .bin) as npmConfigHook left them; cp -r would dereference them.
    installPhase = ''
      runHook preInstall
      mkdir -p $out
      cp -a node_modules $out/node_modules
      runHook postInstall
    '';
  };
  # Build the Hugo site inside the Nix sandbox, so:
  #
  #   HUGO_ENABLEGITINFO=false   no .git in the sandbox; git metadata is
  #                              cosmetic (last-modified dates).
  #   HUGO_ENVIRONMENT=production   selects the PostCSS+PurgeCSS CSS pipeline.
  #   baseURL                    the site is served under the /docs path of
  #                              modelplane.ai (the marketing site proxies
  #                              /docs/* here), so every Permalink, canonical
  #                              tag, asset, and sitemap URL must carry the
  #                              /docs prefix. Only the production artifact is
  #                              served there; `hugo server` (docs-serve) keeps
  #                              the baseURL = "/" from hugo.toml for local dev.
  #                              Vercel PR previews override HUGO_BASEURL with
  #                              the preview's own URL so the deployment is
  #                              self-contained and reviewable (it is served at
  #                              the deployment root, not under /docs). Pure
  #                              flake eval returns "" for getEnv, so CI and
  #                              production builds keep the canonical URL and
  #                              stay reproducible/cached; previews pass
  #                              --impure (see docs/vercel-build.sh).
  #
  # PostCSS resolves plugins from node_modules via NODE_PATH, and Hugo finds
  # the postcss CLI through the node_modules/.bin on PATH.
  mkSite =
    {
      name,
      baseURL,
    }:
    pkgs.runCommand name
      {
        nativeBuildInputs = [
          pkgs.hugo
          pkgs.nodejs
        ];
        env = {
          HUGO_ENABLEGITINFO = "false";
          HUGO_ENVIRONMENT = "production";
          HUGO_BASEURL = baseURL;
        };
      }
      ''
        cp -r ${self}/docs src
        cp -r ${self}/apis apis
        chmod -R u+w src
        cd src
        ln -s ${nodeModules}/node_modules node_modules
        export PATH="$PWD/node_modules/.bin:$PATH"
        export NODE_PATH="$PWD/node_modules"
        hugo --minify --destination "$out"
      '';

  # Vale lints prose against a set of style packages (Google, Microsoft, etc.)
  # that it normally downloads with `vale sync`. The sandbox has no network, so
  # we sync them in a fixed-output derivation instead: it is allowed network
  # access because its output is verified by hash. The packages resolve to
  # GitHub "releases/latest" zips, so the hash changes when any package
  # publishes a new release; rerun the build and update outputHash when Nix
  # reports a mismatch. Local styles (Modelplane/) and the vocabulary live in
  # the repo and are merged in at lint time, not here.
  valeStyles =
    pkgs.runCommand "modelplane-docs-vale-styles"
      {
        nativeBuildInputs = [
          pkgs.vale
          pkgs.cacert
        ];
        outputHashMode = "recursive";
        outputHashAlgo = "sha256";
        outputHash = "sha256-Vq7P/K9kQSj21QeKSAVuOM8aWQ1Yrdu4oAuZg1SCUmg=";
      }
      ''
        export HOME=$TMPDIR
        # .vale.ini's relative "StylesPath = styles" resolves next to the config
        # file, so copy it here for vale to sync into $PWD/styles.
        cp ${self}/docs/utils/vale/.vale.ini .vale.ini
        vale sync --config="$PWD/.vale.ini"
        mkdir -p $out
        cp -r styles/* $out/
      '';
in
{
  # The built static site, served under modelplane.ai/docs/.
  site = mkSite {
    name = "modelplane-docs";
    baseURL =
      let
        envBaseURL = builtins.getEnv "HUGO_BASEURL";
      in
      if envBaseURL != "" then envBaseURL else "https://modelplane.ai/docs/";
  };

  # Lint docs prose with Vale. Merges the network-synced style packages with the
  # repo's local Modelplane style and vocabulary into one StylesPath, then lints
  # offline.
  vale =
    pkgs.runCommand "modelplane-docs-vale"
      {
        nativeBuildInputs = [
          pkgs.vale
          pkgs.findutils
        ];
      }
      ''
        export HOME=$TMPDIR
        # .vale.ini sets a relative "StylesPath = styles", resolved next to the
        # config file. Assemble the config and a merged styles/ here so vale
        # picks up both the synced packages and the repo's local styles.
        cp ${self}/docs/utils/vale/.vale.ini .vale.ini
        mkdir styles
        cp -r ${valeStyles}/* styles/
        cp -r ${self}/docs/utils/vale/styles/* styles/
        find ${self}/docs/content -name '*.md' -print0 | \
          xargs -0 --no-run-if-empty \
            vale --config="$PWD/.vale.ini"
        mkdir -p $out
        touch $out/.vale-passed
      '';

  # Check internal links with htmltest against a site built with the local
  # baseURL ("/" from hugo.toml). htmltest resolves links relative to the site
  # root, so it must not run against the production artifact, whose links carry
  # the /docs prefix. CheckExternal is false in .htmltest.yml, so this needs no
  # network.
  htmltest =
    pkgs.runCommand "modelplane-docs-htmltest"
      {
        nativeBuildInputs = [ pkgs.htmltest ];
      }
      ''
        htmltest --conf ${self}/docs/utils/htmltest/.htmltest.yml \
          ${mkSite {
            name = "modelplane-docs-local";
            baseURL = "/";
          }}
        mkdir -p $out
        touch $out/.htmltest-passed
      '';
}

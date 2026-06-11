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
in
{
  # The built static site. Runs inside the Nix sandbox, so:
  #
  #   HUGO_ENABLEGITINFO=false   no .git in the sandbox; git metadata is
  #                              cosmetic (last-modified dates).
  #   HUGO_ENVIRONMENT=production   selects the PostCSS+PurgeCSS CSS pipeline.
  #
  # PostCSS resolves plugins from node_modules via NODE_PATH, and Hugo finds
  # the postcss CLI through the node_modules/.bin on PATH.
  site =
    pkgs.runCommand "modelplane-docs"
      {
        nativeBuildInputs = [
          pkgs.hugo
          pkgs.nodejs
        ];
        env = {
          HUGO_ENABLEGITINFO = "false";
          HUGO_ENVIRONMENT = "production";
        };
      }
      ''
        cp -r ${self}/docs src
        chmod -R u+w src
        cd src
        ln -s ${nodeModules}/node_modules node_modules
        export PATH="$PWD/node_modules/.bin:$PATH"
        export NODE_PATH="$PWD/node_modules"
        hugo --minify --destination "$out"
      '';
}

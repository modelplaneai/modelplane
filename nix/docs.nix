# Docs checks that run against the content in this repo.
#
# The Hugo project that renders this content - config, layouts, theme, asset
# pipelines - is the docs-site repo, which is also what builds and deploys the
# published site. What lives here is the content and the linting of it, so a
# prose change fails CI in the repo where it was written.
{ pkgs, self }:
let
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
        outputHash = "sha256-WbPAE0+fnV7gqU7P4c9qKWETRHTR7uP5OpsHCj+l9r4=";
      }
      ''
        export HOME=$TMPDIR
        cp ${self}/docs/utils/vale/.vale.ini .vale.ini
        # Since Vale 3.12, sync installs packages into the user data directory
        # rather than the config's StylesPath, and that directory is
        # platform-specific: XDG on Linux, Application Support on macOS. Copy
        # from wherever it landed, so this builds on a maintainer's Mac as well
        # as in CI.
        vale sync --config="$PWD/.vale.ini"
        mkdir -p $out
        for styles in \
          "$HOME/.local/share/vale/styles" \
          "$HOME/Library/Application Support/vale/styles"; do
          if [ -d "$styles" ]; then
            cp -r "$styles"/* $out/
          fi
        done
      '';
in
{
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
}

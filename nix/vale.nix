# Vale checks, which lint the repo's prose with the config in .vale.ini.
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
  styles =
    pkgs.runCommand "modelplane-vale-styles"
      {
        nativeBuildInputs = [
          pkgs.vale
          pkgs.cacert
        ];
        outputHashMode = "recursive";
        outputHashAlgo = "sha256";
        outputHash = "sha256-2eRYliSjeVajgP5xCpXjyc62DgZ9PBLfXqBCdOdL/SQ=";
      }
      ''
        export HOME=$TMPDIR
        cp ${self}/.vale.ini .vale.ini
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
  # Lint the markdown under dirs. .vale.ini's sections match paths relative to
  # the repo root, so this runs from there, with the config and a merged
  # StylesPath assembled outside it: the synced packages, plus the repo's own
  # Modelplane style and vocabulary.
  lint =
    name: dirs:
    pkgs.runCommand "modelplane-${name}-vale"
      {
        nativeBuildInputs = [
          pkgs.vale
          pkgs.findutils
        ];
      }
      ''
        export HOME=$TMPDIR
        config=$TMPDIR/vale
        mkdir -p $config/.vale/styles
        cp ${self}/.vale.ini $config/.vale.ini
        # The synced packages come out of the store read-only, and ai-tells
        # ships a config/ directory that the repo's vocabularies merge into.
        cp -r --no-preserve=mode ${styles}/* $config/.vale/styles/
        cp -r ${self}/.vale/styles/Modelplane ${self}/.vale/styles/config $config/.vale/styles/
        cd ${self}
        find ${pkgs.lib.escapeShellArgs dirs} -name '*.md' -print0 | \
          xargs -0 --no-run-if-empty \
            vale --config="$config/.vale.ini"
        mkdir -p $out
        touch $out/.vale-passed
      '';
}

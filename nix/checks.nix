# CI checks (nix flake check).
#
# All checks run inside the Nix sandbox without network or filesystem access.
# Unit tests run against uv2nix-built venvs with test sources copied from the
# flake source tree.
{
  pkgs,
  self,
  functionNames,
  pyproject-nix,
  uv2nix,
  pyproject-build-systems,
}:
let
  docs = import ./docs.nix { inherit pkgs self; };

  workspace = uv2nix.lib.workspace.loadWorkspace { workspaceRoot = self; };
  pythonSet =
    (pkgs.callPackage pyproject-nix.build.packages { python = pkgs.python312; }).overrideScope
      (
        pkgs.lib.composeManyExtensions [
          pyproject-build-systems.overlays.wheel
          (workspace.mkPyprojectOverlay { sourcePreference = "wheel"; })
        ]
      );

  # Each function exports a 'function' Python module, so tests must run from
  # a directory where that module is importable via the venv. We copy tests/
  # from the source tree and run unittest against the venv's Python.
  mkFunctionTest =
    name:
    let
      venv = pythonSet.mkVirtualEnv "${name}-test-env" {
        ${name} = [ ];
      };
    in
    pkgs.runCommand "modelplane-test-${name}" { } ''
      cp -r ${self}/functions/${name}/tests tests
      ${venv}/bin/python -m unittest discover -s tests -v
      mkdir -p $out
      touch $out/.tests-passed
    '';
in
{
  # Verify the docs site builds. The build is the check.
  docs = docs.site;

  # Lint docs prose with Vale.
  docs-vale = docs.vale;

  # Check docs internal links with htmltest.
  docs-htmltest = docs.htmltest;

  python =
    pkgs.runCommand "modelplane-python-checks"
      {
        nativeBuildInputs = [ pkgs.ruff ];
      }
      ''
        cp -r ${self} src
        chmod -R u+w src
        cd src
        ruff format --check functions/
        ruff check functions/
        mkdir -p $out
        touch $out/.python-checks-passed
      '';

  shell-lint =
    pkgs.runCommand "modelplane-shell-lint"
      {
        nativeBuildInputs = [
          pkgs.findutils
          pkgs.shellcheck
          pkgs.shfmt
        ];
      }
      ''
        cd ${self}
        find . -name '*.sh' -type f | while read -r script; do
          shellcheck "$script"
          shfmt -d "$script"
        done
        mkdir -p $out
        touch $out/.shell-lint-passed
      '';

  nix-lint =
    pkgs.runCommand "modelplane-nix-lint"
      {
        nativeBuildInputs = [
          pkgs.statix
          pkgs.deadnix
          pkgs.nixfmt-rfc-style
        ];
      }
      ''
        statix check ${self}
        deadnix --fail ${self}/flake.nix ${self}/nix
        nixfmt --check ${self}/flake.nix ${self}/nix/*.nix
        mkdir -p $out
        touch $out/.nix-lint-passed
      '';

  # Fail if uv.lock is out of sync with any pyproject.toml in the workspace.
  # uv lock --check resolves the workspace against the lockfile without
  # writing it. The sandbox has no network, no writable HOME, and no
  # /bin/sh for uv's interpreter discovery, so:
  #
  #   --offline                  skip the network
  #   UV_CACHE_DIR=...           write cache into the build dir
  #   --no-managed-python        don't try to download a Python
  #   --python ${pkgs.python3}/bin/python   use the nix-provided interpreter
  uv-lock =
    pkgs.runCommand "modelplane-uv-lock"
      {
        nativeBuildInputs = [
          pkgs.unstable.uv
          pkgs.python3
        ];
        env.UV_CACHE_DIR = "uv-cache";
      }
      ''
        cp -r ${self} src
        chmod -R u+w src
        cd src
        uv lock --check --offline \
          --no-managed-python \
          --python ${pkgs.python3}/bin/python
        mkdir -p $out
        touch $out/.uv-lock-passed
      '';
}
// builtins.listToAttrs (
  map (name: {
    name = "test-${name}";
    value = mkFunctionTest name;
  }) functionNames
)

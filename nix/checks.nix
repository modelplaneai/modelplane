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

  # Type-check each function with ty. Each function exports its own 'function'
  # module, so checking all functions at once would let ty resolve one
  # function's `function.fn` import to another's package. We check each in
  # isolation against a venv that provides its dependencies, plus the protobuf
  # type stubs ty needs to resolve the SDK's generated Struct and Duration.
  #
  # Unlike mkFunctionTest, which runs the function module from the venv, ty
  # checks the source, so we copy function/ and tests/ from the tree. We also
  # copy pyproject.toml: the sandbox has no parent tree for ty to discover the
  # [tool.ty] config in, where the target Python version is set.
  mkFunctionTypeCheck =
    name:
    let
      venv = pythonSet.mkVirtualEnv "${name}-ty-env" {
        ${name} = [ ];
        types-protobuf = [ ];
      };
    in
    pkgs.runCommand "modelplane-ty-${name}"
      {
        nativeBuildInputs = [ pkgs.unstable.ty ];
      }
      ''
        cp -r ${self}/functions/${name}/function function
        cp -r ${self}/functions/${name}/tests tests
        cp ${self}/pyproject.toml pyproject.toml
        ty check function tests --python ${venv}
        mkdir -p $out
        touch $out/.ty-passed
      '';
in
{
  # Verify the docs site builds. The build is the check.
  docs = docs.site;

  # Lint docs prose with Vale.
  docs-vale = docs.vale;

  # Check docs internal links with htmltest.
  docs-htmltest = docs.htmltest;

  # Validate the example manifests the docs show against the generated Pydantic
  # models, so an example that drifts from the live API schema fails CI. Covers
  # everything under docs/manifests/, including the API-reference examples under
  # docs/manifests/reference/. Reuses compose-inference-gateway's venv, which
  # already provides crossplane-models, pydantic, and pyyaml.
  docs-manifests =
    let
      venv = pythonSet.mkVirtualEnv "docs-manifests-validate-env" {
        compose-inference-gateway = [ ];
      };
    in
    pkgs.runCommand "modelplane-docs-manifests" { } ''
      cp -r ${self} src
      chmod -R u+w src
      cd src
      ${venv}/bin/python docs/utils/validate/validate_manifests.py
      mkdir -p $out
      touch $out/.docs-manifests-validated
    '';

  python =
    pkgs.runCommand "modelplane-python-checks"
      {
        nativeBuildInputs = [ pkgs.unstable.ruff ];
      }
      ''
        cp -r ${self} src
        chmod -R u+w src
        cd src
        ruff format --check functions/ docs/utils/validate/
        ruff check functions/ docs/utils/validate/
        mkdir -p $out
        touch $out/.python-checks-passed
      '';

  # Fail if any hand-written source file is missing its Apache 2.0 license
  # header. Scoped to the files we author: the composition functions and the
  # docs manifest validator. Generated models under schemas/python carry their
  # own codegen banner, and config (*.toml) and vendored upstream CRDs (*.yaml)
  # are excluded. addlicense -check only reads, so it runs against the store
  # path directly. Run 'nix run .#fix' to add any missing headers.
  license =
    pkgs.runCommand "modelplane-license-check"
      {
        nativeBuildInputs = [ pkgs.addlicense ];
      }
      ''
        cd ${self}
        addlicense -check \
          -ignore '**/*.toml' \
          -ignore '**/*.yaml' \
          -ignore '**/*.yml' \
          functions/ docs/utils/validate/ nix.sh docs/vercel-build.sh
        mkdir -p $out
        touch $out/.license-check-passed
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
// builtins.listToAttrs (
  map (name: {
    name = "ty-${name}";
    value = mkFunctionTypeCheck name;
  }) functionNames
)

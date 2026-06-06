# OCI image tarballs for each composition function.
#
# Builds one image per (function, architecture) pair using uv2nix. The uv.lock
# at the workspace root is the source of truth for Python dependencies.
#
# The resulting tarballs are consumed by `crossplane project build` via the
# Tarball function source in crossplane-project.yaml.
{
  pkgs,
  self,
  functionNames,
  pyproject-nix,
  uv2nix,
  pyproject-build-systems,
}:
let
  architectures = [
    "amd64"
    "arm64"
  ];

  archToNixSystem = {
    "amd64" = "x86_64-linux";
    "arm64" = "aarch64-linux";
  };

  workspace = uv2nix.lib.workspace.loadWorkspace { workspaceRoot = self; };

  # Build a Python package set for a given architecture. Uses pkgsCross when
  # the target differs from the host so the Python interpreter matches the
  # target while build tools (hatchling etc.) run on the host.
  #
  # Uses overlays.wheel (not overlays.default) because the default overlay
  # builds hatchling from source, which is missing pathspec as a native build
  # input for cross-compilation targets.
  pythonSetForArch =
    arch:
    let
      targetSystem = archToNixSystem.${arch};
      targetPkgs =
        if targetSystem == pkgs.system then
          pkgs
        else
          pkgs.pkgsCross.${
            {
              "x86_64-linux" = "gnu64";
              "aarch64-linux" = "aarch64-multiplatform";
            }
            .${targetSystem}
          };
    in
    (targetPkgs.callPackage pyproject-nix.build.packages { python = targetPkgs.python312; })
    .overrideScope
      (
        pkgs.lib.composeManyExtensions [
          pyproject-build-systems.overlays.wheel
          (workspace.mkPyprojectOverlay { sourcePreference = "wheel"; })
          # TODO(negz): Remove this overlay once the function-sdk-python git pin
          # in pyproject.toml is dropped (after #208 is released). It's only
          # needed because the git pin is built from sdist with hatchling, which
          # the wheel overlay doesn't provide; inject it as a build-system input.
          (final: prev: {
            crossplane-function-sdk-python = prev.crossplane-function-sdk-python.overrideAttrs (old: {
              nativeBuildInputs = (old.nativeBuildInputs or [ ]) ++ final.resolveBuildSystem { hatchling = [ ]; };
            });
          })
        ]
      );

  etcPasswd = pkgs.writeTextDir "etc/passwd" ''
    root:x:0:0:root:/root:/sbin/nologin
    nonroot:x:65532:65532:nonroot:/home/nonroot:/sbin/nologin
  '';
  etcGroup = pkgs.writeTextDir "etc/group" ''
    root:x:0:
    nonroot:x:65532:
  '';

  mkFunctionImage =
    { name, arch }:
    let
      venv = (pythonSetForArch arch).mkVirtualEnv "${name}-env" {
        ${name} = [ ];
      };
    in
    pkgs.dockerTools.buildLayeredImage {
      name = "${name}-${arch}";
      tag = "latest";
      architecture = arch;
      contents = [
        venv
        etcPasswd
        etcGroup
      ];
      config = {
        Entrypoint = [ "${venv}/bin/function" ];
        User = "nonroot:nonroot";
        WorkingDir = "/";
        ExposedPorts = {
          "9443/tcp" = { };
        };
      };
    };

  images = builtins.listToAttrs (
    builtins.concatMap (
      name:
      map (arch: {
        name = "${name}-${arch}";
        value = mkFunctionImage { inherit name arch; };
      }) architectures
    ) functionNames
  );

  all = pkgs.runCommand "modelplane-functions" { } ''
    mkdir -p $out
    ${builtins.concatStringsSep "\n" (
      builtins.attrValues (
        builtins.mapAttrs (key: img: ''
          ln -s ${img} $out/${key}.tar.gz
        '') images
      )
    )}
  '';
in
{
  inherit images all;
}

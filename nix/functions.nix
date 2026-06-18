# OCI image tarballs for each composition function.
#
# Builds one image per (function, architecture) pair. The uv.lock at the
# workspace root is the source of truth for Python dependencies.
#
# Images are assembled without running the target-arch Python, so any arch's
# image builds on any host - Linux x86, Linux arm, or macOS - with no
# cross-compilation and no QEMU emulation. For a given architecture we
# reference:
#
#   * the target-arch CPython, a Nix store path substituted from cache.nixos.org
#   * the target-arch wheels, fixed-output downloads from PyPI (uv.lock pins
#     their hashes)
#   * our own pure-Python source (the function and the generated schemas)
#
# We assemble these into a site-packages tree with `uv pip install`, passing
# --python-platform to target the image's arch rather than the build host's. uv
# selects and lays out wheels by reading their metadata - it never runs the
# target interpreter - so the build needs no cross-compilation or QEMU.
# --no-deps and --offline with the exact wheels uv2nix resolved keep it pinned
# and hermetic.
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
  inherit (pkgs) lib;

  architectures = [
    "amd64"
    "arm64"
  ];

  archToNixSystem = {
    "amd64" = "x86_64-linux";
    "arm64" = "aarch64-linux";
  };

  archToUvArch = {
    "amd64" = "x86_64";
    "arm64" = "aarch64";
  };

  pythonVersion = "3.12";

  # uv's --python-platform for a target arch, e.g. "x86_64-manylinux_2_40". The
  # glibc baseline is taken from the interpreter we actually ship, so uv accepts
  # every wheel that interpreter can load (manylinux tags <= our glibc). Some
  # deps, e.g. google-re2, only publish wheels for a fairly recent glibc.
  uvPlatform =
    targetPkgs: arch:
    let
      glibc = lib.versions.majorMinor targetPkgs.stdenv.cc.libc.version;
    in
    "${archToUvArch.${arch}}-manylinux_${lib.replaceStrings [ "." ] [ "_" ] glibc}";

  # uv runs on the build host, so take it from the host package set.
  inherit (pkgs.unstable) uv;

  workspace = uv2nix.lib.workspace.loadWorkspace { workspaceRoot = self; };

  # A uv2nix package set for the target arch's nixpkgs. We use it only as data:
  # to resolve each function's dependency closure and reference the target-arch
  # CPython and wheel sources. We never call mkVirtualEnv.
  #
  # overlays.wheel (not overlays.default) so dependencies resolve to prebuilt
  # wheels rather than sdists that would need building.
  pythonSetForArch =
    arch:
    let
      targetSystem = archToNixSystem.${arch};
      targetPkgs =
        if targetSystem == pkgs.system then pkgs else import pkgs.path { system = targetSystem; };
    in
    {
      inherit targetPkgs;
      set =
        (targetPkgs.callPackage pyproject-nix.build.packages { python = targetPkgs.python312; })
        .overrideScope
          (
            lib.composeManyExtensions [
              pyproject-build-systems.overlays.wheel
              (workspace.mkPyprojectOverlay { sourcePreference = "wheel"; })
            ]
          );
    };

  etcPasswd = pkgs.writeTextDir "etc/passwd" ''
    root:x:0:0:root:/root:/sbin/nologin
    nonroot:x:65532:65532:nonroot:/home/nonroot:/sbin/nologin
  '';
  etcGroup = pkgs.writeTextDir "etc/group" ''
    root:x:0:
    nonroot:x:65532:
  '';

  # Host Python carrying the build backends our workspace packages declare
  # (uv_build for the functions, hatchling for crossplane-models). With
  # --no-build-isolation, uv imports them from here instead of building an
  # isolated env.
  buildBackends = pkgs.python312.withPackages (ps: [
    ps.uv-build
    ps.hatchling
  ]);

  # Build a function's site-packages tree with a single `uv pip install`. uv2nix
  # gives us the resolved closure: prebuilt wheels for third-party deps, source
  # trees for our own workspace members (which uv builds from their backends).
  mkSitePackages =
    { name, arch }:
    let
      inherit (pythonSetForArch arch) targetPkgs set;
      resolved = set.resolveVirtualEnv { ${name} = [ ]; };

      # A package's src is a prebuilt wheel or a source tree, which uv takes on
      # the command line differently: a wheel must be staged under its original
      # filename (the Nix store hash prefix breaks uv's version parsing), a
      # source tree is passed as-is.
      isWheel = p: (p ? src) && (p.src ? name);

      # uv2nix builds each wheel's src with the target arch's fetchurl, so the
      # download derivation carries system = the target arch. A wheel download is
      # a fixed-output derivation - content-addressed and byte-identical on every
      # system - but Nix still refuses to *build* one whose system doesn't match
      # the host. On an x86_64 host with no aarch64 substitute available that
      # leaves the arm64 image unbuildable. Re-fetch each wheel with the host's
      # fetchurl, preserving name, url, and hash: same output path, but a
      # derivation that builds on whatever host we're on.
      hostWheel =
        src:
        pkgs.fetchurl {
          inherit (src) name url;
          hash = src.outputHash;
        };
      wheels = map (p: hostWheel p.src) (builtins.filter isWheel resolved);
      sources = map (p: p.src) (builtins.filter (p: !isWheel p) resolved);

      # uv needs an interpreter it can run, to resolve the install and to build
      # our pure-Python source packages under --no-build-isolation. It must not
      # be the target arch's interpreter: uv queries it by executing it, which
      # fails (Exec format error) when the target arch isn't the build host's
      # and there's no emulation. We use the build host's CPython instead, of
      # the same minor version as the target's, and let --python-platform and
      # --python-version describe the target. --target writes a flat layout that
      # doesn't depend on the resolving interpreter's install scheme, so the
      # result is the same wherever it's built.
      hostPython = pkgs.python312;
    in
    pkgs.runCommand "${name}-${arch}-site-packages"
      {
        nativeBuildInputs = [ uv ];
      }
      ''
        # Stage wheels under their original filenames (see isWheel).
        mkdir wheels
        ${lib.concatMapStringsSep "\n" (w: "cp ${w} wheels/${w.name}") wheels}

        mkdir -p $out
        export HOME=$TMPDIR
        # Make the build backends importable for the --no-build-isolation
        # source builds.
        export PYTHONPATH=${buildBackends}/${buildBackends.sitePackages}

        # --python is the build-host interpreter uv runs to resolve the install;
        # --python-platform and --python-version describe the target image's
        # arch and Python, selecting the wheels uv lays out.
        uv pip install \
          --no-deps --offline --no-cache --link-mode=copy \
          --no-build-isolation \
          --python ${hostPython}/bin/python${pythonVersion} \
          --python-platform ${uvPlatform targetPkgs arch} \
          --python-version ${pythonVersion} \
          --target $out \
          wheels/*.whl ${lib.concatStringsSep " " (map toString sources)}

        # uv writes console-script wrappers under bin/ whose shebang points at
        # --python, i.e. the build-host interpreter. That interpreter is the
        # build host's arch, not the image's, so shipping bin/ would drag a
        # second, wrong-arch CPython (and its glibc) into the image. The image
        # runs `python -m function.main`, never these wrappers, so drop them.
        rm -rf $out/bin

        # uv records each source install's provenance in a PEP 610
        # direct_url.json under .dist-info, as a file:// URL into the workspace
        # source tree (the flake's `self`). Nix scans the output for store
        # paths, finds that hash, and drags the entire repository - apis, design
        # docs, the docs site - into the image's runtime closure as a "source"
        # layer. Nothing reads this metadata at runtime, so drop it.
        find $out -name direct_url.json -path '*.dist-info/*' -delete
      '';

  mkFunctionImage =
    { name, arch }:
    let
      inherit (pythonSetForArch arch) targetPkgs;
      sitePackages = mkSitePackages { inherit name arch; };
      python = targetPkgs.python312;
    in
    pkgs.dockerTools.buildLayeredImage {
      name = "${name}-${arch}";
      tag = "latest";
      architecture = arch;
      contents = [
        python
        sitePackages
        targetPkgs.stdenv.cc.cc.lib # libstdc++.so.6, libgcc_s.so.1 for wheels
        etcPasswd
        etcGroup
      ];
      config = {
        Entrypoint = [
          "${python}/bin/python${pythonVersion}"
          "-m"
          "function.main"
        ];
        User = "nonroot:nonroot";
        WorkingDir = "/";
        Env = [
          "PYTHONPATH=${sitePackages}"
          # The manylinux wheels' extensions carry no RPATH, so their libs need
          # to be on the loader path. The interpreter finds its own via RPATH.
          "LD_LIBRARY_PATH=${lib.makeLibraryPath [ targetPkgs.stdenv.cc.cc.lib ]}"
        ];
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

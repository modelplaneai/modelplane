# Build derivations for Modelplane.
#
# All builders are functions that take an attrset of arguments and return a
# derivation. The actual build definitions live in flake.nix.
{ pkgs, self }:
let
  # The up CLI isn't in nixpkgs. Fetch the binary from Upbound's CDN.
  upVersion = "0.44.3";
  upBins = {
    "x86_64-linux" = {
      url = "https://cli.upbound.io/stable/v${upVersion}/bin/linux_amd64/up";
      hash = "sha256-tvPmftejC2Pcsjn8kYf5DfPPUYHEtK5kQlQCJfyM7uc=";
    };
    "aarch64-linux" = {
      url = "https://cli.upbound.io/stable/v${upVersion}/bin/linux_arm64/up";
      hash = "sha256-gnJht2k343zPNr2qpoPQtTBgeVro4fyfJWs1idzaM1M=";
    };
    "x86_64-darwin" = {
      # TODO(negz): Prefetch and verify this hash.
      url = "https://cli.upbound.io/stable/v${upVersion}/bin/darwin_amd64/up";
      hash = "sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=";
    };
    "aarch64-darwin" = {
      url = "https://cli.upbound.io/stable/v${upVersion}/bin/darwin_arm64/up";
      hash = "sha256-Z2lbmnDxhgXDh+JN6yxIYtelQ2//Pg/HHHCgXQZBh/g=";
    };
  };

  # The Docker credential helper for xpkg.upbound.io. up shells out to it to
  # authenticate when fetching package dependencies.
  dockerCredentialUpBins = {
    "x86_64-linux" = {
      url = "https://cli.upbound.io/stable/v${upVersion}/bin/linux_amd64/docker-credential-up";
      hash = "sha256-weGga6mxaNqoJx1X+mgtaOlxeXSRdHBSGUjX82V8S9A=";
    };
    "aarch64-linux" = {
      url = "https://cli.upbound.io/stable/v${upVersion}/bin/linux_arm64/docker-credential-up";
      hash = "sha256-3r3ZqIAKIAt/Ec9WSRbVt/rnut1k+Kx4mLNjPgTtzUc=";
    };
    "x86_64-darwin" = {
      # TODO(negz): Prefetch and verify this hash.
      url = "https://cli.upbound.io/stable/v${upVersion}/bin/darwin_amd64/docker-credential-up";
      hash = "sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=";
    };
    "aarch64-darwin" = {
      url = "https://cli.upbound.io/stable/v${upVersion}/bin/darwin_arm64/docker-credential-up";
      hash = "sha256-mSIOSuc/nKgo0hw66gpVrZlGR0l+7ZAu16YBb/4K/GE=";
    };
  };
in
{
  # The Upbound up CLI.
  up =
    { system }:
    let
      bin = upBins.${system};
    in
    pkgs.stdenvNoCC.mkDerivation {
      pname = "up";
      version = upVersion;
      src = pkgs.fetchurl {
        inherit (bin) url hash;
      };
      dontUnpack = true;
      installPhase = ''
        install -Dm755 $src $out/bin/up
      '';
    };

  # The docker-credential-up binary. Required by up to authenticate with
  # xpkg.upbound.io when fetching package dependencies.
  dockerCredentialUp =
    { system }:
    let
      bin = dockerCredentialUpBins.${system};
    in
    pkgs.stdenvNoCC.mkDerivation {
      pname = "docker-credential-up";
      version = upVersion;
      src = pkgs.fetchurl {
        inherit (bin) url hash;
      };
      dontUnpack = true;
      installPhase = ''
        install -Dm755 $src $out/bin/docker-credential-up
      '';
    };

  # The web UI frontend (Vite + React).
  frontend =
    { version }:
    pkgs.buildNpmPackage {
      pname = "modelplane-ui-frontend";
      inherit version;
      src = "${self}/ui/frontend";
      npmDepsHash = "sha256-zIma/8cqbWJKZN55ASsvBghT1LJvX6x63Z92j8R5W+Y=";
      installPhase = ''
        runHook preInstall
        cp -r dist $out
        runHook postInstall
      '';
    };

  # The web UI Go proxy binary. The frontend is copied into
  # internal/web/static/ before building so embed.FS picks it up.
  proxy =
    { version, frontend }:
    pkgs.buildGoModule {
      pname = "modelplane-ui";
      inherit version;
      src = "${self}/ui";
      vendorHash = "sha256-NYX6KEuOvfDUyPG3sUehXqMETIkJDDQhKlAAra3/hQA=";
      subPackages = [ "cmd/proxy" ];
      env.CGO_ENABLED = "0";

      overrideModAttrs = _: {
        postPatch = ''
          mkdir -p internal/web/static
        '';
      };
      postPatch = ''
        rm -rf internal/web/static
        cp -r ${frontend} internal/web/static
      '';
    };

  # The web UI OCI container image.
  image =
    { proxy }:
    let
      passwd = pkgs.writeText "passwd" ''
        root:x:0:0:root:/root:/sbin/nologin
        nonroot:x:65532:65532:nonroot:/home/nonroot:/sbin/nologin
      '';
      group = pkgs.writeText "group" ''
        root:x:0:
        nonroot:x:65532:
      '';
    in
    pkgs.dockerTools.buildLayeredImage {
      name = "modelplane-ui";
      tag = "latest";
      contents = [
        proxy
        pkgs.cacert
      ];
      extraCommands = ''
        mkdir -p tmp home/nonroot etc
        chmod 1777 tmp
        cp ${passwd} etc/passwd
        cp ${group} etc/group
      '';
      config = {
        Entrypoint = [ "${proxy}/bin/proxy" ];
        ExposedPorts = {
          "8080/tcp" = { };
        };
        User = "65532";
        Env = [
          "SSL_CERT_FILE=${pkgs.cacert}/etc/ssl/certs/ca-certificates.crt"
        ];
        Labels = {
          "org.opencontainers.image.source" = "https://github.com/modelplaneai/modelplane";
        };
      };
    };
}

# New to Nix? Start here:
#   Language basics:  https://nix.dev/tutorials/nix-language
#   Flakes intro:     https://zero-to-nix.com/concepts/flakes
{
  description = "Modelplane - The open source control plane for AI models";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-25.11";
  };

  outputs =
    { self, nixpkgs }:
    let
      supportedSystems = [
        "x86_64-linux"
        "aarch64-linux"
        "x86_64-darwin"
        "aarch64-darwin"
      ];

      forAllSystems = f: nixpkgs.lib.genAttrs supportedSystems (system: forSystem system f);
      forSystem =
        system: f:
        f {
          inherit system;
          pkgs = import nixpkgs { inherit system; };
        };

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

      mkUp =
        pkgs: system:
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

      # Build the frontend (Vite + React).
      mkFrontend =
        pkgs:
        pkgs.buildNpmPackage {
          pname = "modelplane-ui-frontend";
          version = "0.1.0";
          src = ./ui/frontend;
          npmDepsHash = "sha256-EC/m8CuocYTKehAl8ONdm+8rXS9w8ajyLyc2u28mKNM=";
          installPhase = ''
            runHook preInstall
            cp -r dist $out
            runHook postInstall
          '';
        };

      # Build the Go proxy binary. The frontend is copied into
      # internal/web/static/ before building so embed.FS picks it up.
      mkProxy =
        pkgs:
        let
          frontend = mkFrontend pkgs;
        in
        pkgs.buildGoModule {
          pname = "modelplane-ui";
          version = "0.1.0";
          src = ./ui;
          vendorHash = "sha256-NYX6KEuOvfDUyPG3sUehXqMETIkJDDQhKlAAra3/hQA=";
          subPackages = [ "cmd/proxy" ];
          env.CGO_ENABLED = "0";

          # Copy the built frontend into the embed directory before building.
          # Uses overrideAttrs on the go-modules (vendor) derivation to add the
          # same step there — otherwise the sandbox can't find static/.
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

      # Build the OCI image.
      mkImage =
        pkgs:
        let
          proxy = mkProxy pkgs;
          # Minimal /etc/passwd and /etc/group for nonroot.
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

    in
    {
      # Build outputs (nix build).
      packages = forAllSystems (
        { pkgs, ... }:
        {
          default = mkImage pkgs;
          proxy = mkProxy pkgs;
          frontend = mkFrontend pkgs;
        }
      );

      # Development commands (nix run .#<app>).
      apps = forAllSystems (
        { pkgs, system, ... }:
        {
          # Load the image into Docker: nix run .#load-image
          load-image = {
            type = "app";
            program = toString (
              pkgs.writeShellScript "load-image" ''
                ${pkgs.docker-client}/bin/docker load < ${mkImage pkgs}
                echo "Loaded modelplane-ui:latest"
              ''
            );
          };

          # Lint Go code: nix run .#lint
          lint = {
            type = "app";
            program = toString (
              pkgs.writeShellScript "lint" ''
                cd ui
                ${pkgs.golangci-lint}/bin/golangci-lint run ./...
              ''
            );
          };

          # Run Crossplane composition tests: nix run .#test-crossplane
          # These are effectively E2E tests — they need Docker for the
          # function-python runtime, so they can't run in the Nix sandbox.
          test-crossplane = {
            type = "app";
            program = toString (
              pkgs.writeShellScript "test-crossplane" ''
                set -euo pipefail
                echo "Building the Crossplane project..."
                ${(mkUp pkgs system)}/bin/up project build
                echo ""
                echo "Running composition tests..."
                ${(mkUp pkgs system)}/bin/up test run tests/*
              ''
            );
          };
        }
      );

      # CI checks (nix flake check).
      checks = forAllSystems (
        { pkgs, ... }:
        let
          # Go checks reuse buildGoModule to prefetch dependencies into the
          # sandbox. The checkPhase runs after the build, with full access to
          # the vendor directory.
          goChecks = pkgs.buildGoModule {
            pname = "modelplane-ui-checks";
            version = "0.1.0";
            src = ./ui;
            vendorHash = "sha256-NYX6KEuOvfDUyPG3sUehXqMETIkJDDQhKlAAra3/hQA=";
            env.CGO_ENABLED = "0";

            overrideModAttrs = _: {
              postPatch = ''
                mkdir -p internal/web/static
              '';
            };
            postPatch = ''
              mkdir -p internal/web/static
            '';

            nativeBuildInputs = [ pkgs.golangci-lint ];

            # Skip the default build — we only care about checks.
            buildPhase = "true";
            installPhase = "touch $out";

            checkPhase = ''
              export HOME=$(mktemp -d)
              echo "Running Go tests..."
              go test ./internal/...
              echo "Running golangci-lint..."
              golangci-lint run ./...
            '';
          };

          # Frontend checks reuse buildNpmPackage to prefetch node_modules.
          frontendChecks = pkgs.buildNpmPackage {
            pname = "modelplane-ui-frontend-checks";
            version = "0.1.0";
            src = ./ui/frontend;
            npmDepsHash = "sha256-EC/m8CuocYTKehAl8ONdm+8rXS9w8ajyLyc2u28mKNM=";

            buildPhase = ''
              echo "Running TypeScript type check..."
              npx tsc -b --noEmit
              echo "Running Vitest..."
              npx vitest run
            '';
            installPhase = "touch $out";
          };
        in
        {
          go = goChecks;
          frontend = frontendChecks;

          # Python lint doesn't need network — ruff works on source files only.
          python = pkgs.stdenvNoCC.mkDerivation {
            pname = "modelplane-python-checks";
            version = "0.1.0";
            src = ./.;
            nativeBuildInputs = [ pkgs.ruff ];
            buildPhase = ''
              echo "Running ruff..."
              ruff check functions/ tests/
            '';
            installPhase = "touch $out";
          };

          # Nix formatting.
          nix = pkgs.stdenvNoCC.mkDerivation {
            pname = "modelplane-nix-checks";
            version = "0.1.0";
            src = ./.;
            nativeBuildInputs = [ pkgs.nixfmt-rfc-style ];
            buildPhase = ''
              echo "Checking Nix formatting..."
              nixfmt --check flake.nix
            '';
            installPhase = "touch $out";
          };
        }
      );

      devShells = forAllSystems (
        { pkgs, system, ... }:
        {
          default = pkgs.mkShell {
            buildInputs = [
              # Crossplane / Upbound
              (mkUp pkgs system)

              # Kubernetes
              pkgs.kubectl
              pkgs.kubernetes-helm
              pkgs.kind
              pkgs.docker-client

              # Python (for linting composition functions)
              pkgs.python3
              pkgs.ruff
              pkgs.pyright

              # Go (for the web UI proxy)
              pkgs.go
              pkgs.golangci-lint

              # Node.js (for the web UI frontend)
              pkgs.nodejs

              # Nix
              pkgs.nixfmt-rfc-style
            ];

            shellHook = ''
              export PS1='\[\033[38;2;173;123;252m\][model\[\033[38;2;195;158;253m\]plane]\[\033[0m\] \w \$ '

              source <(kubectl completion bash 2>/dev/null)
              source <(helm completion bash 2>/dev/null)
              source <(kind completion bash 2>/dev/null)

              alias k=kubectl

              echo "Modelplane development shell"
              echo ""
              echo "  Crossplane project"
              echo "    up project build                    Build XRDs, functions, and compositions"
              echo "    up test run tests/*                 Run composition tests"
              echo "    ruff check functions/               Lint Python functions"
              echo "    pyright                             Type-check Python functions"
              echo ""
              echo "  Web UI"
              echo "    cd ui && go run ./cmd/proxy \\      Run the proxy (needs --kubeconfig)"
              echo "      --kubeconfig ~/.kube/config"
              echo "    cd ui/frontend && npm run dev       Run the frontend with hot reload"
              echo "    cd ui && golangci-lint run ./...    Lint Go code"
              echo ""
              echo "  Nix"
              echo "    nix build                           Build the web UI container image"
              echo "    nix run .#load-image                Load the image into Docker"
              echo "    nix flake check                     Run all checks (lint, tests)"
              echo "    nix run .#test-crossplane           Run Crossplane composition tests"
              echo ""
            '';
          };
        }
      );
    };
}

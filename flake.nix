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
      # Set by CI to override the auto-generated dev version.
      buildVersion = null;

      supportedSystems = [
        "x86_64-linux"
        "aarch64-linux"
        "x86_64-darwin"
        "aarch64-darwin"
      ];

      # Semantic version for builds. Uses buildVersion if set by CI, otherwise
      # generates a dev version from git metadata. (self ? shortRev tests if the
      # attribute exists - clean commits have shortRev, uncommitted changes have
      # dirtyShortRev.)
      version =
        if buildVersion != null then
          buildVersion
        else if self ? shortRev then
          "v0.0.0-${builtins.toString self.lastModified}-${self.shortRev}"
        else
          "v0.0.0-${builtins.toString self.lastModified}-${self.dirtyShortRev}";

      # Helpers for per-system outputs.
      forAllSystems = f: nixpkgs.lib.genAttrs supportedSystems (system: forSystem system f);
      forSystem =
        system: f:
        f {
          inherit system;
          pkgs = import nixpkgs { inherit system; };
        };

    in
    {
      # Build outputs (nix build).
      packages = forAllSystems (
        { pkgs, ... }:
        let
          build = import ./nix/build.nix { inherit pkgs self; };
          fe = build.frontend { inherit version; };
          px = build.proxy {
            inherit version;
            frontend = fe;
          };
        in
        {
          default = build.image { proxy = px; };
          proxy = px;
          frontend = fe;
        }
      );

      # CI checks (nix flake check).
      checks = forAllSystems (
        { pkgs, ... }:
        let
          checks = import ./nix/checks.nix { inherit pkgs self; };
        in
        {
          go-test = checks.goTest { inherit version; };
          go-lint = checks.goLint { inherit version; };
          frontend = checks.frontend { inherit version; };
          python = checks.python { };
          shell-lint = checks.shellLint { };
          nix-lint = checks.nixLint { };
        }
      );

      # Development commands (nix run .#<app>).
      apps = forAllSystems (
        { pkgs, system, ... }:
        let
          build = import ./nix/build.nix { inherit pkgs self; };
          apps = import ./nix/apps.nix { inherit pkgs; };
          up = build.up { inherit system; };
          dockerCredentialUp = build.dockerCredentialUp { inherit system; };
          fe = build.frontend { inherit version; };
          px = build.proxy {
            inherit version;
            frontend = fe;
          };
        in
        {
          build-crossplane = apps.buildCrossplane { inherit up dockerCredentialUp; };
          test-crossplane = apps.testCrossplane { inherit up dockerCredentialUp; };
          push-crossplane = apps.pushCrossplane { inherit up dockerCredentialUp; };
          lint = apps.lint { };
          load-image = apps.loadImage { image = build.image { proxy = px; }; };
          dev-proxy = apps.devProxy { };
          dev-frontend = apps.devFrontend { };
        }
      );

      # Development shell (nix develop).
      devShells = forAllSystems (
        { pkgs, system, ... }:
        let
          build = import ./nix/build.nix { inherit pkgs self; };
          up = build.up { inherit system; };
          dockerCredentialUp = build.dockerCredentialUp { inherit system; };
        in
        {
          default = pkgs.mkShell {
            buildInputs = [
              # Crossplane / Upbound
              up
              dockerCredentialUp

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
              echo "  nix run .#build-crossplane    nix run .#dev-proxy"
              echo "  nix run .#test-crossplane     nix run .#dev-frontend"
              echo "  nix run .#push-crossplane     nix run .#load-image"
              echo ""
              echo "  nix build                     nix flake check"
              echo "  nix flake show"
              echo ""
            '';
          };
        }
      );
    };
}

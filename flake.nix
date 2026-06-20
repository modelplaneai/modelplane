# New to Nix? Start here:
#   Language basics:  https://nix.dev/tutorials/nix-language
#   Flakes intro:     https://zero-to-nix.com/concepts/flakes
{
  description = "Modelplane - The open source control plane for AI models";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-25.11";

    # Unstable nixpkgs, exposed as pkgs.unstable. Used when we need a
    # newer version of a package than the stable channel ships, e.g. uv
    # tracking the latest uv_build releases.
    nixpkgs-unstable.url = "github:NixOS/nixpkgs/nixos-unstable";

    # Pinned to crossplane/cli main rather than a released version, because we
    # depend on several CLI changes that aren't in a release yet:
    #
    #   * #24, #64: a datamodel-code-generator bump that fixes Python model
    #     generation for fields named int/bool.
    #   * #119: stops an XRD's scale subresource from clobbering the generated
    #     model with the autoscaling Scale type.
    #   * #126: makes the flake's default package the host-native CLI binary
    #     instead of the full multi-platform release bundle. Building one platform
    #     instead of seven cuts the CLI build from ~55 minutes to ~8 on a cold
    #     machine.
    #   * #127: decompresses function runtime tarballs once when loading them,
    #     rather than once per layer.
    #
    # Repin to a tag once these all release.
    crossplane-cli.url = "github:crossplane/cli";

    # uv2nix reads a uv workspace's uv.lock and generates Nix derivations
    # for each Python package, using pyproject.nix's build infrastructure.
    pyproject-nix = {
      url = "github:pyproject-nix/pyproject.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    uv2nix = {
      url = "github:pyproject-nix/uv2nix";
      inputs = {
        pyproject-nix.follows = "pyproject-nix";
        nixpkgs.follows = "nixpkgs";
      };
    };
    pyproject-build-systems = {
      url = "github:pyproject-nix/build-system-pkgs";
      inputs = {
        pyproject-nix.follows = "pyproject-nix";
        uv2nix.follows = "uv2nix";
        nixpkgs.follows = "nixpkgs";
      };
    };
  };

  outputs =
    {
      self,
      nixpkgs,
      nixpkgs-unstable,
      crossplane-cli,
      pyproject-nix,
      uv2nix,
      pyproject-build-systems,
    }:
    let
      # Set by CI to override the auto-generated dev version.
      buildVersion = null;

      # The composition functions that make up Modelplane.
      functionNames = [
        "compose-eks-cluster"
        "compose-gke-cluster"
        "compose-inference-class"
        "compose-inference-cluster"
        "compose-inference-gateway"
        "compose-serving-stack"
        "compose-model-cache"
        "compose-model-deployment"
        "compose-model-endpoint"
        "compose-model-replica"
        "compose-model-service"
        "compose-usages"
      ];

      supportedSystems = [
        "x86_64-linux"
        "aarch64-linux"
        "x86_64-darwin"
        "aarch64-darwin"
      ];

      # Semantic version for packages. Uses buildVersion if set by CI,
      # otherwise generates a dev version from git metadata.
      version =
        if buildVersion != null then
          buildVersion
        else if self ? shortRev then
          "v0.1.0-dev.${builtins.toString self.lastModified}.g${self.shortRev}"
        else
          "v0.1.0-dev.${builtins.toString self.lastModified}.g${self.dirtyShortRev}";

      forAllSystems = f: nixpkgs.lib.genAttrs supportedSystems (system: forSystem system f);
      forSystem =
        system: f:
        f {
          inherit system;
          pkgs = import nixpkgs {
            inherit system;
            config.allowUnfreePredicate = pkg: builtins.elem (nixpkgs.lib.getName pkg) [ "upbound" ];
            overlays = [
              (_: _: {
                unstable = import nixpkgs-unstable {
                  inherit system;
                };
              })
            ];
          };
        };

    in
    {
      checks = forAllSystems (
        { pkgs, ... }:
        import ./nix/checks.nix {
          inherit
            pkgs
            self
            functionNames
            pyproject-nix
            uv2nix
            pyproject-build-systems
            ;
        }
      );

      # Build the docs site with nix build .#docs.
      #
      # Function runtime images are Linux images, but they're assembled purely
      # from data (a cached interpreter, prebuilt wheels, and our source), so
      # they build on any host - including macOS - with no cross-compilation or
      # emulation. Build individual images with nix build .#<function>-<arch>,
      # or all of them with nix build .#functions.
      packages = forAllSystems (
        { pkgs, ... }:
        let
          docs = import ./nix/docs.nix { inherit pkgs self; };
          functions = import ./nix/functions.nix {
            inherit
              pkgs
              self
              functionNames
              pyproject-nix
              uv2nix
              pyproject-build-systems
              ;
          };
        in
        {
          docs = docs.site;
        }
        // functions.images
        // {
          functions = functions.all;
        }
      );

      apps = forAllSystems (
        { pkgs, system, ... }:
        let
          deps = import ./nix/deps.nix { inherit pkgs crossplane-cli; };
          apps = import ./nix/apps.nix { inherit pkgs; };
          crossplane = deps.crossplane { inherit system; };
          functionsPkg = self.packages.${system}.functions or null;
        in
        {
          fix = apps.fix { };
          build = apps.build {
            inherit crossplane functionsPkg;
            dockerCredentialUp = pkgs.upbound;
          };
          push = apps.push {
            inherit crossplane version;
            dockerCredentialUp = pkgs.upbound;
          };
          run = apps.run {
            inherit crossplane functionsPkg;
            dockerCredentialUp = pkgs.upbound;
          };
          stop = apps.stop { inherit crossplane; };
          docs-serve = apps.docsServe { };
          docs-generate = apps.docsGenerate { };
        }
      );

      devShells = forAllSystems (
        { pkgs, system, ... }:
        let
          deps = import ./nix/deps.nix { inherit pkgs crossplane-cli; };
          crossplane = deps.crossplane { inherit system; };
        in
        {
          default = pkgs.mkShell {
            buildInputs = [
              crossplane
              pkgs.upbound
              pkgs.kubectl
              pkgs.kubernetes-helm
              pkgs.kind
              pkgs.docker-client
              pkgs.unstable.uv
              pkgs.python3
              pkgs.ruff
              pkgs.nixfmt-rfc-style
              pkgs.hugo
              pkgs.nodejs
            ];

            shellHook = ''
              export PS1='\[\033[38;2;173;123;252m\][model\[\033[38;2;195;158;253m\]plane]\[\033[0m\] \w \$ '

              source <(kubectl completion bash 2>/dev/null)
              source <(helm completion bash 2>/dev/null)
              source <(kind completion bash 2>/dev/null)

              alias k=kubectl

              echo "Modelplane development shell"
              echo ""
              echo "  nix flake check               nix run .#fix"
              echo "  nix run .#build               nix run .#push"
              echo "  nix run .#run                 nix run .#stop"
              echo "  nix run .#docs-serve          nix run .#docs-generate"
              echo ""
            '';
          };
        }
      );
    };
}

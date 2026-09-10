# External dependencies not available in nixpkgs.
#
# The Crossplane CLI comes from the master channel of the release CDN, not
# the crossplane/cli flake: we need `crossplane project run --no-default-mrap`,
# which no tagged release ships yet. To bump: pick a version from
# https://cli.crossplane.io/master/current/version and refresh the hashes:
#
#   for p in linux_amd64 linux_arm64 darwin_arm64; do
#     curl -sL "https://cli.crossplane.io/master/<version>/bin/$p/crossplane" | sha256sum
#   done
{ pkgs, ... }:
let
  version = "v2.6.0-rc.0.73.ga588ce6";
  channel = "master";

  hashes = {
    linux_amd64 = "8184e02bbb419652f475fe8f3dd9681d2c62549563209c27c41e169437f2fb8f";
    linux_arm64 = "6c4d38e9a903beb72d2f45feffa0f9f1b5e95be986165ecfa1bd8426b98137ed";
    darwin_arm64 = "180ee3796aca6e8ae98ddf339c5b23e8220434064b867970dd67b6a2c05b2970";
  };

  platforms = {
    x86_64-linux = "linux_amd64";
    aarch64-linux = "linux_arm64";
    aarch64-darwin = "darwin_arm64";
  };
in
{
  crossplane =
    { system }:
    let
      platform = platforms.${system};
    in
    pkgs.stdenvNoCC.mkDerivation {
      pname = "crossplane-cli";
      inherit version;

      src = pkgs.fetchurl {
        url = "https://cli.crossplane.io/${channel}/${version}/bin/${platform}/crossplane";
        sha256 = hashes.${platform};
      };

      dontUnpack = true;

      installPhase = ''
        runHook preInstall
        install -Dm755 $src $out/bin/crossplane
        runHook postInstall
      '';

      meta = {
        description = "CLI for interacting with Crossplane";
        homepage = "https://docs.crossplane.io/latest/cli/";
        license = pkgs.lib.licenses.asl20;
        mainProgram = "crossplane";
      };
    };
}

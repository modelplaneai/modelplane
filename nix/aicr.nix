# The NVIDIA AICR CLI, from the GitHub release tarballs. nixpkgs has no
# aicr package, and the serving stack generator needs the exact release
# its catalog fork was synced against.
#
# The version here and AICR_PIN in
# functions/compose-serving-stack/generate.py move together: the
# generator asserts `aicr --version` matches its pin and fails closed on
# a mismatch. To bump, follow "Bumping aicr" in CONTRIBUTING.md and
# refresh the hashes from the release's aicr_checksums.txt:
#
#   https://github.com/NVIDIA/aicr/releases/download/v<version>/aicr_checksums.txt
_final: prev:
let
  version = "1.0.0";

  hashes = {
    linux_amd64 = "965ad7be114e78c18371e1bf032001eecad811d382d252be1a00be781241af9b";
    linux_arm64 = "f50d64a69549009e6ad14e6479ccf11787bdb1f8f9384296a4d35fc19893d67a";
    darwin_arm64 = "cb85cce54a82deb88e826c3e735c26e11a0863173b8aa5ff3ba4ebc2dc6af8b2";
  };

  platform =
    {
      x86_64-linux = "linux_amd64";
      aarch64-linux = "linux_arm64";
      aarch64-darwin = "darwin_arm64";
    }
    .${prev.stdenv.hostPlatform.system};
in
{
  aicr = prev.stdenvNoCC.mkDerivation {
    pname = "aicr";
    inherit version;

    src = prev.fetchurl {
      url = "https://github.com/NVIDIA/aicr/releases/download/v${version}/aicr_${version}_${platform}.tar.gz";
      sha256 = hashes.${platform};
    };

    # The tarball is flat (the binary and sigstore attestations at the
    # root), so there is no top-level directory for the unpacker to cd
    # into.
    sourceRoot = ".";

    installPhase = ''
      runHook preInstall
      install -Dm755 aicr $out/bin/aicr
      runHook postInstall
    '';

    meta = {
      description = "CLI for NVIDIA AI Cluster Runtime recipes";
      homepage = "https://github.com/NVIDIA/aicr";
      license = prev.lib.licenses.asl20;
      mainProgram = "aicr";
    };
  };
}

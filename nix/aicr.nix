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
  version = "0.20.0";

  hashes = {
    linux_amd64 = "a80a7ed1ad7474434c929efbea77223b0eb156f901569319698e9bdb9e1126f9";
    linux_arm64 = "f8353a56ff430714818879c8b4c4de057c0e3cea11beb34f45e4753db8f300f5";
    darwin_arm64 = "0e1b735f91383f0ba130aedf2b83299d0255587474e80dabf0392e194460a846";
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

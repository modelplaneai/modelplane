# External dependencies not available in nixpkgs.
{ crossplane-cli, ... }:
{
  # The Crossplane CLI. The upstream flake's default package is the host-native
  # binary for the current system, so we can use it as-is.
  crossplane = { system }: crossplane-cli.packages.${system}.default;
}

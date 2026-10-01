# The uv workspace's Python packages, built by uv2nix from uv.lock, for
# virtualenvs that run on the build host. The function images build their own,
# per target architecture (see functions.nix).
{
  pkgs,
  self,
  pyproject-nix,
  uv2nix,
  pyproject-build-systems,
}:
let
  workspace = uv2nix.lib.workspace.loadWorkspace { workspaceRoot = self; };
in
(pkgs.callPackage pyproject-nix.build.packages { python = pkgs.python312; }).overrideScope (
  pkgs.lib.composeManyExtensions [
    pyproject-build-systems.overlays.wheel
    (workspace.mkPyprojectOverlay { sourcePreference = "wheel"; })
  ]
)

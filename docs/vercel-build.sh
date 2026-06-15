#!/usr/bin/env bash
# Build the docs site on Vercel using Nix.
#
# Vercel's build image is fixed (Amazon Linux 2023) and can't be swapped for a
# Nix image, so we install Nix into it and build the same .#docs derivation
# that CI verifies. The build runs as root in an ephemeral container, so we do
# a single-user install with the nixbld build-users group disabled. cache.nixos.org
# supplies almost everything prebuilt, so a cold build is seconds, not minutes.
#
# Vercel runs installCommand and buildCommand as separate shells, so this
# script does both in one place and vercel.json calls it as the buildCommand.
set -euo pipefail

# Pin the Nix installer to an immutable, versioned release rather than the
# rolling nixos.org/nix/install redirect, and verify its SHA-256 before
# running it, so a swapped artifact or a MITM can't get code execution here.
# This script embeds and checks the per-arch tarball hashes itself, so pinning
# it pins the whole install. Bump both together when upgrading Nix:
#   curl -fsSL https://releases.nixos.org/nix/nix-<ver>/install | sha256sum
NIX_VERSION="2.31.2"
NIX_INSTALLER_SHA256="078e2ffeddf6a9c1f22adf41458ccc46a58bb26911a9e01579645314f9982994"

# Nix's single-user installer and git both need a few tools the base image
# doesn't ship. --skip-broken sidesteps the curl-minimal/curl conflict.
dnf install -y --skip-broken tar xz git >/dev/null

# Build as the current user, with no nixbld group. The installer reads NIX_CONFIG
# while installing, so set it before the install too.
export NIX_CONFIG="experimental-features = nix-command flakes
build-users-group ="

# Pre-create /nix so the installer doesn't try to use sudo (absent here).
mkdir -p /nix
chmod 0755 /nix
chown "$(id -u)" /nix

installer="$(mktemp)"
curl -fsSL "https://releases.nixos.org/nix/nix-${NIX_VERSION}/install" -o "$installer"
echo "${NIX_INSTALLER_SHA256}  ${installer}" | sha256sum --check --status
sh "$installer" --no-daemon --yes --no-channel-add
rm -f "$installer"

export PATH="/nix/var/nix/profiles/default/bin:$PATH"

# Build the site. result is a symlink into the read-only store; dereference it
# into a plain, writable directory Vercel can serve.
#
# Production keeps the canonical https://modelplane.ai/docs/ baseURL baked into
# nix/docs.nix: a pure, cached build identical to what CI verifies. PR previews
# are served standalone at the deployment's own root (not under /docs), so we
# rebuild with the preview URL as the baseURL, making links and assets resolve
# against the preview itself instead of production. --impure lets the
# derivation read HUGO_BASEURL from the environment (getEnv is "" in pure eval).
if [ "${VERCEL_ENV:-}" = "production" ]; then
	nix build .#docs --print-build-logs
else
	export HUGO_BASEURL="https://${VERCEL_URL}/"
	nix build .#docs --impure --print-build-logs
fi
rm -rf public
cp -rL --no-preserve=mode result public
rm -f result

#!/usr/bin/env bash
# Copyright 2026 The Modelplane Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

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

# Vercel's Amazon Linux 2023 build image doesn't provide /dev/fd, which bash
# process substitution (< <(...)) needs. patchelf's setup-hook uses it during
# the fixup phase of any derivation containing ELF files — e.g. the fetchNpmDeps
# FOD, whose npm cache includes native bindings like lightningcss. Without this
# symlink, patchelf fails with "/dev/fd/63: No such file or directory" and the
# entire docs build cascades to failure. See:
#   https://github.com/NixOS/nixpkgs/blob/master/pkgs/build-support/patchelf/setup-hook.sh
[ -e /dev/fd ] || ln -s /proc/self/fd /dev/fd

# The Vercel project's Root Directory is docs/, so the api/ serverless functions
# live under the docs site rather than polluting the repo root. The build runs
# with the working directory at docs/, but the flake that defines .#docs is at
# the repo root, so build from there. This needs the project's "Include source
# files outside of the Root Directory in the Build Step" setting enabled, which
# checks out the parent into the build container.
repo_root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$repo_root"

# Build the site. result is a symlink into the read-only store; dereference it
# into a plain, writable directory Vercel can serve.
#
# Production keeps the canonical https://docs.modelplane.ai/ baseURL baked into
# nix/docs.nix: a pure, cached build identical to what CI verifies.
#
# Previews rebuild with a root-relative ("/") baseURL. A preview is reachable on
# several hostnames (the per-deployment URL and the branch alias) and sits behind
# Vercel Deployment Protection, so an absolute baseURL baked to one host breaks
# the page when it's viewed on another: the cross-host request for the
# stylesheet/JS hits the auth wall and returns HTML instead of the asset, leaving
# the page unstyled. Root-relative URLs resolve against whatever host serves the
# page, so the preview renders correctly on all of them. --impure lets the
# derivation read HUGO_BASEURL from the environment (getEnv is "" in pure eval).
if [ "${VERCEL_ENV:-}" = "production" ]; then
	nix build .#docs --print-build-logs
else
	export HUGO_BASEURL="/"
	nix build .#docs --impure --print-build-logs
fi
# outputDirectory in docs/vercel.json is "public", resolved against the docs/
# Root Directory, so the served site goes to docs/public.
rm -rf docs/public
cp -rL --no-preserve=mode result docs/public
rm -f result

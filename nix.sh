#!/usr/bin/env bash
# nix.sh - Run Nix commands via Docker without installing Nix locally.
#
# Usage: ./nix.sh <command>
#
# Run './nix.sh flake show' for available outputs, or see flake.nix.
# Examples: ./nix.sh develop, ./nix.sh develop --command ruff check functions/
#
# The first run downloads dependencies into /nix/store (cached in a Docker
# volume). Subsequent runs reuse the cache. To reset: docker volume rm modelplane-nix

set -e

# When NIX_SH_CONTAINER is set, we're running inside the Docker container.
# This script re-executes itself inside the container to avoid sh -c quoting.

if [ "${NIX_SH_CONTAINER:-}" = "1" ]; then
	# The container runs as root, but the bind-mounted /modelplane is owned by
	# the host user. Git refuses to operate in directories owned by other users.
	git config --global --add safe.directory /modelplane

	# Record the current time. After nix runs, we'll find files newer than this
	# marker and chown them to the host user.
	marker=$(mktemp)

	nix "${@}"

	# Fix ownership of any files nix created or modified. The container runs as
	# root, so without this, generated files would be root-owned on the host.
	# Using -newer is surgical - we only chown files touched during this run.
	find /modelplane -newer "${marker}" -exec chown "${HOST_UID}:${HOST_GID}" {} + 2>/dev/null || true
	rm -f "${marker}"

	exit 0
fi

# When running on the host, launch a Docker container and re-execute this
# script inside it.

# Nix configuration, equivalent to /etc/nix/nix.conf.
NIX_CONFIG="
# Flakes are Nix's modern project format - a flake.nix file plus a flake.lock
# that pins all dependencies. This is still marked 'experimental' but is stable
# and widely used.
experimental-features = nix-command flakes

# Build multiple derivations in parallel. A derivation is Nix's build unit,
# like a Makefile target. 'auto' uses one job per CPU core.
max-jobs = auto

# Sandbox builds to prevent access to undeclared dependencies. Requires --privileged.
sandbox = true
"

# Only allocate a TTY if stdout is a terminal. TTY mode corrupts binary output.
# The -i flag keeps stdin open for interactive commands like 'nix develop'.
INTERACTIVE_FLAGS=""
if [ -t 1 ]; then
	INTERACTIVE_FLAGS="-it"
fi

docker run --rm --privileged --cgroupns=host ${INTERACTIVE_FLAGS} \
	-v "$(pwd):/modelplane" \
	-v "modelplane-nix:/nix" \
	-w /modelplane \
	-e "NIX_SH_CONTAINER=1" \
	-e "NIX_CONFIG=${NIX_CONFIG}" \
	-e "HOST_UID=$(id -u)" \
	-e "HOST_GID=$(id -g)" \
	-e "TERM=${TERM:-xterm}" \
	nixos/nix \
	/modelplane/nix.sh "${@}"

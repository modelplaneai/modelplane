#!/usr/bin/env bash
# Cleanup all demo resources so you can re-run the demo from scratch.
# Safe to run even if resources don't exist.
set -euo pipefail

echo "==> Deleting demo deployments"
kubectl delete modeldeployment --all -n ml-team --ignore-not-found

echo "==> Waiting for placements to be cleaned up"
sleep 5
kubectl get modelplacements -n ml-team 2>/dev/null || true

echo "==> Deleting demo catalog models"
kubectl delete clustermodel --all --ignore-not-found

echo ""
echo "==> Cleanup complete. Run setup.sh to prepare for another run."

from __future__ import annotations

"""Kubernetes client wrapper for modelplane CRD operations."""

import sys

import click
from kubernetes import client, config
from kubernetes.client.exceptions import ApiException

from mp import resources as res


def _api() -> client.CustomObjectsApi:
    """Load kubeconfig and return a CustomObjectsApi client."""
    try:
        config.load_kube_config()
    except config.config_exception.ConfigException:
        try:
            config.load_incluster_config()
        except config.config_exception.ConfigException:
            click.echo("Error: Could not connect to Kubernetes cluster.", err=True)
            click.echo("Make sure your kubeconfig is set up (ask your platform team).", err=True)
            sys.exit(1)
    return client.CustomObjectsApi()


def _core_api() -> client.CoreV1Api:
    """Return a CoreV1Api client (kubeconfig must already be loaded)."""
    return client.CoreV1Api()


def get_cluster_resource(plural: str, name: str) -> dict | None:
    """Get a single cluster-scoped CRD. Returns None if not found."""
    api = _api()
    try:
        return api.get_cluster_custom_object(res.GROUP, res.VERSION, plural, name)
    except ApiException as e:
        if e.status == 404:
            return None
        raise


def list_namespaced_resources(plural: str, namespace: str) -> list[dict]:
    """List namespace-scoped CRDs."""
    api = _api()
    result = api.list_namespaced_custom_object(res.GROUP, res.VERSION, namespace, plural)
    return result.get("items", [])


def get_namespaced_resource(plural: str, name: str, namespace: str) -> dict | None:
    """Get a single namespace-scoped CRD. Returns None if not found."""
    api = _api()
    try:
        return api.get_namespaced_custom_object(res.GROUP, res.VERSION, namespace, plural, name)
    except ApiException as e:
        if e.status == 404:
            return None
        raise


def create_namespaced_resource(plural: str, namespace: str, body: dict) -> dict:
    """Create a namespace-scoped CRD."""
    api = _api()
    return api.create_namespaced_custom_object(res.GROUP, res.VERSION, namespace, plural, body)


def delete_namespaced_resource(plural: str, name: str, namespace: str) -> None:
    """Delete a namespace-scoped CRD."""
    api = _api()
    api.delete_namespaced_custom_object(res.GROUP, res.VERSION, namespace, plural, name)


def ensure_namespace(namespace: str) -> None:
    """Create namespace if it doesn't exist."""
    core = _core_api()
    try:
        core.read_namespace(namespace)
    except ApiException as e:
        if e.status == 404:
            core.create_namespace(client.V1Namespace(metadata=client.V1ObjectMeta(name=namespace)))
        else:
            raise


def get_current_namespace() -> str:
    """Get the current namespace from kubeconfig context."""
    try:
        _, active_context = config.list_kube_config_contexts()
        return active_context.get("context", {}).get("namespace", "default")
    except config.config_exception.ConfigException:
        return "default"

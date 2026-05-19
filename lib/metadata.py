"""Kubernetes metadata constants shared across composition functions.

Labels, namespaces, and resource names used by multiple functions or that
benefit from a single source of truth even when used by only one.
"""

# Label keys. All in the modelplane.ai domain.
LABEL_KEY_CLUSTER = "modelplane.ai/cluster"
LABEL_KEY_DEPLOYMENT = "modelplane.ai/deployment"
LABEL_KEY_GPU = "modelplane.ai/gpu"
LABEL_KEY_POOL = "modelplane.ai/pool"
LABEL_KEY_RELEASE = "modelplane.ai/release"
LABEL_KEY_REPLICA = "modelplane.ai/replica"
LABEL_KEY_RESOURCE = "modelplane.ai/resource"

# Label values for presence labels (key=true).
LABEL_VALUE_CLUSTER = "true"
LABEL_VALUE_REPLICA = "true"

# Namespaces.
NAMESPACE_SYSTEM = "modelplane-system"
NAMESPACE_REMOTE = "default"

# The control plane gateway name. Used as the Gateway resource name,
# the MetalLB IP pool name, and the HTTPRoute parentRef.
GATEWAY_NAME = "modelplane"

# Scheme used for gateway-facing URLs. Inference traffic between the
# control plane gateway and remote cluster gateways uses plain HTTP;
# TLS terminates at the edge.
GATEWAY_SCHEME = "http"

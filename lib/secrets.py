"""Secret type and key constants shared across composition functions.

The GKECluster function writes secrets to XR status with these types. Other
functions (compose-inference-env, compose-kserve-stack) match on the type
string to find the right secret. Changing a type here without updating all
consumers would silently break the lookup.
"""

# Secret types written by compose-gke-cluster, read by compose-inference-env
# and compose-kserve-stack.
SECRET_TYPE_KUBECONFIG = "Kubeconfig"
SECRET_TYPE_GCP_SA_KEY = "GCPServiceAccountKey"

# Secret keys. These are the keys within the Kubernetes Secret objects
# created by the GCP providers. Used when building ProviderConfig secretRefs.
SECRET_KEY_KUBECONFIG = "kubeconfig"
SECRET_KEY_GCP_SA = "private_key"

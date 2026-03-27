"""Secret type constants shared across composition functions.

The GKECluster function writes secrets to XR status with these types. Other
functions (compose-inference-env, compose-kserve-stack) match on the type
string to find the right secret. Changing a type here without updating all
consumers would silently break the lookup.
"""

SECRET_TYPE_KUBECONFIG = "Kubeconfig"
SECRET_TYPE_GCP_SA_KEY = "GCPServiceAccountKey"

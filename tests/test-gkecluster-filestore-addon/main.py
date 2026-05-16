"""Test GKECluster with the Filestore CSI addon opted in.

When spec.addons.gcpFilestoreCsiDriver is True, compose-gke-cluster
should compose a ProjectService MR that enables file.googleapis.com
on the project. Without this, the in-cluster Filestore CSI driver
installs but every provisioning call returns SERVICE_DISABLED and
PVCs sit Pending forever — symptom is silent because the cluster
itself comes up healthy.

This test covers compose_project_services and the mark_readiness
extension that tracks projectservice-filestore so the XR can only
report Ready once the API enable is observed Ready too.
"""

from .lib import resource as libresource
from .model.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1
from .model.io.upbound.dev.meta.compositiontest import v1alpha1 as compositiontest
from .model.io.upbound.m.gcp.cloudplatform.projectservice import (
    v1beta1 as projectservicev1beta1,
)

test = compositiontest.CompositionTest(
    metadata=metav1.ObjectMeta(
        name="gkecluster-filestore-addon",
    ),
    spec=compositiontest.Spec(
        compositionPath="apis/gkeclusters/composition.yaml",
        xrPath="tests/test-gkecluster-filestore-addon/xr.yaml",
        xrdPath="apis/gkeclusters/definition.yaml",
        timeoutSeconds=120,
        validate=False,
        assertResources=[
            # Assert a ProjectService MR is composed enabling
            # file.googleapis.com on the project. disableOnDestroy=False
            # so tearing down one InferenceCluster doesn't yank an API
            # the rest of the project relies on.
            libresource.model_to_dict(
                projectservicev1beta1.ProjectService(
                    metadata=metav1.ObjectMeta(
                        annotations={
                            "crossplane.io/composition-resource-name": "projectservice-filestore",
                        },
                    ),
                    spec=projectservicev1beta1.Spec(
                        forProvider=projectservicev1beta1.ForProvider(
                            project="acme-ml-platform",
                            service="file.googleapis.com",
                            disableOnDestroy=False,
                        ),
                    ),
                )
            ),
        ],
    ),
)

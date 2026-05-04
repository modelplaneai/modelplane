from .model.ai.modelplane.inferencegateway import v1alpha1 as igwv1alpha1
from .model.io.k8s.api.core import v1 as corev1
from .model.io.k8s.api.rbac import v1 as rbacv1
from .model.io.k8s.apimachinery.pkg.apis.core.meta import v1 as coremetav1
from .model.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1
from .model.io.upbound.dev.meta.e2etest import v1alpha1 as e2etestv1alpha1


def _fixture(model) -> dict:
    data = model.model_dump(exclude_none=True, warnings=False)
    if hasattr(model, "apiVersion") and model.apiVersion is not None:
        data["apiVersion"] = model.apiVersion
    if hasattr(model, "kind") and model.kind is not None:
        data["kind"] = model.kind
    return data


test = e2etestv1alpha1.E2ETest(
    metadata=metav1.ObjectMeta(name="e2etest-inferencegateway"),
    spec=e2etestv1alpha1.Spec(
        crossplane=e2etestv1alpha1.Crossplane(
            autoUpgrade=e2etestv1alpha1.AutoUpgrade(channel="Stable"),
        ),
        defaultConditions=["Ready"],
        timeoutSeconds=600,
        cleanupTimeoutSeconds=300,
        skipDelete=False,
        initResources=[
            # Shared namespace for Modelplane infrastructure.
            _fixture(corev1.Namespace(
                metadata=coremetav1.ObjectMeta(name="modelplane-system"),
            )),

            # RBAC aggregation so Crossplane can compose Gateway API, MetalLB,
            # and Usage resources.
            _fixture(rbacv1.ClusterRole(
                metadata=coremetav1.ObjectMeta(
                    name="crossplane-compose-modelplane",
                    labels={"rbac.crossplane.io/aggregate-to-crossplane": "true"},
                ),
                rules=[
                    rbacv1.PolicyRule(apiGroups=[""], resources=["namespaces"], verbs=["*"]),
                    rbacv1.PolicyRule(
                        apiGroups=["gateway.networking.k8s.io"],
                        resources=["gateways", "gatewayclasses", "httproutes"],
                        verbs=["*"],
                    ),
                    rbacv1.PolicyRule(
                        apiGroups=["gateway.envoyproxy.io"],
                        resources=["backends"],
                        verbs=["*"],
                    ),
                    rbacv1.PolicyRule(
                        apiGroups=["metallb.io"],
                        resources=["ipaddresspools", "l2advertisements"],
                        verbs=["*"],
                    ),
                    rbacv1.PolicyRule(
                        apiGroups=["protection.crossplane.io"],
                        resources=["usages"],
                        verbs=["*"],
                    ),
                ],
            )),

            # Grant cluster-admin to all provider SAs in crossplane-system.
            # The DRC/ImageConfig SA-naming pattern has a race condition in
            # up test run --local (package deployment is created before the
            # ImageConfig reconciler can update runtimeConfigRef). Using the
            # Group subject covers any auto-generated SA name.
            _fixture(rbacv1.ClusterRoleBinding(
                metadata=coremetav1.ObjectMeta(name="provider-helm-modelplane"),
                roleRef=rbacv1.RoleRef(
                    apiGroup="rbac.authorization.k8s.io",
                    kind="ClusterRole",
                    name="cluster-admin",
                ),
                subjects=[rbacv1.Subject(
                    kind="Group",
                    apiGroup="rbac.authorization.k8s.io",
                    name="system:serviceaccounts:crossplane-system",
                )],
            )),
        ],
        manifests=[
            _fixture(igwv1alpha1.InferenceGateway(
                metadata=metav1.ObjectMeta(name="default"),
                spec=igwv1alpha1.Spec(
                    backend="EnvoyGateway",
                    envoyGateway=igwv1alpha1.EnvoyGateway(
                        version="v1.3.0",
                        loadBalancer="MetalLB",
                        metallb=igwv1alpha1.Metallb(
                            addressPool="172.18.255.200-172.18.255.250",
                        ),
                    ),
                    gateway=igwv1alpha1.Gateway(port=80),
                ),
            )),
        ],
    ),
)

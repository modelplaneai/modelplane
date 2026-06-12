"""NVIDIA Dynamo backend — designed-for, not built in v0.1.

The dispatcher never selects this in v0.1 (no Dynamo-only capability is wired).
When built, build() will emit a DynamoGraphDeployment (nvidia.com/v1alpha1)
Object reconciled by the Dynamo operator installed by ServingStack.
"""

from models.ai.modelplane.modelreplica import v1alpha1

from function.backends import base


class DynamoBackend:
    def build(
        self,
        replica: v1alpha1.ModelReplica,
        engine,
        provider_config: str,
        serving_label: str,
    ) -> dict[str, base.ComposedResource]:
        raise NotImplementedError("the Dynamo backend is not implemented in v0.1")

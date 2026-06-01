"""Tests for the compose-kserve-backend function."""

import unittest

from crossplane.function import logging, resource
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from function import fn
from google.protobuf import duration_pb2 as durationpb
from google.protobuf import json_format
from google.protobuf import struct_pb2 as structpb
from models.ai.modelplane.infrastructure.kservebackend import v1alpha1
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as metav1


def setUpModule() -> None:
    logging.configure(level=logging.Level.DISABLED)


# The kustomize storage patch value, as it appears in function output.
_KUSTOMIZE_STORAGE_PATCH = '{"patches": [{"patch": "{\\"apiVersion\\": \\"v1\\", \\"kind\\": \\"ConfigMap\\", \\"metadata\\": {\\"name\\": \\"inferenceservice-config\\"}, \\"data\\": {\\"storageInitializer\\": \\"{\\\\\\"image\\\\\\": \\\\\\"kserve/storage-initializer:latest\\\\\\", \\\\\\"memoryRequest\\\\\\": \\\\\\"100Mi\\\\\\", \\\\\\"memoryLimit\\\\\\": \\\\\\"4Gi\\\\\\", \\\\\\"cpuRequest\\\\\\": \\\\\\"100m\\\\\\", \\\\\\"cpuLimit\\\\\\": \\\\\\"1\\\\\\", \\\\\\"caBundleConfigMapName\\\\\\": \\\\\\"\\\\\\", \\\\\\"caBundleVolumeMountPath\\\\\\": \\\\\\"/etc/ssl/custom-certs\\\\\\", \\\\\\"enableModelcar\\\\\\": true, \\\\\\"cpuModelcar\\\\\\": \\\\\\"10m\\\\\\", \\\\\\"memoryModelcar\\\\\\": \\\\\\"15Mi\\\\\\", \\\\\\"uidModelcar\\\\\\": 1010}\\"}}", "target": {"kind": "ConfigMap", "name": "inferenceservice-config"}}]}'

# Precomputed child_name values for test-backend.
_PC_NAME = "test-backend-cluster-63fde"
_STORAGE_PATCH_NAME = "test-backend-storage-patch-bc0a3"

# The InferenceModel CRD manifest, used in cases 2 and 3.
_INFERENCE_MODEL_CRD = {
    "apiVersion": "apiextensions.k8s.io/v1",
    "kind": "CustomResourceDefinition",
    "metadata": {
        "annotations": {
            "controller-gen.kubebuilder.io/version": "v0.16.1",
        },
        "name": "inferencemodels.inference.networking.x-k8s.io",
    },
    "spec": {
        "group": "inference.networking.x-k8s.io",
        "names": {
            "kind": "InferenceModel",
            "listKind": "InferenceModelList",
            "plural": "inferencemodels",
            "singular": "inferencemodel",
        },
        "scope": "Namespaced",
        "versions": [
            {
                "additionalPrinterColumns": [
                    {
                        "jsonPath": ".spec.modelName",
                        "name": "Model Name",
                        "type": "string",
                    },
                    {
                        "jsonPath": ".spec.poolRef.name",
                        "name": "Inference Pool",
                        "type": "string",
                    },
                    {
                        "jsonPath": ".spec.criticality",
                        "name": "Criticality",
                        "type": "string",
                    },
                    {
                        "jsonPath": ".metadata.creationTimestamp",
                        "name": "Age",
                        "type": "date",
                    },
                ],
                "name": "v1alpha2",
                "schema": {
                    "openAPIV3Schema": {
                        "description": "InferenceModel is the Schema for the InferenceModels API.",
                        "properties": {
                            "apiVersion": {
                                "description": "APIVersion defines the versioned schema of this representation of an object.\nServers should convert recognized schemas to the latest internal value, and\nmay reject unrecognized values.\nMore info: https://git.k8s.io/community/contributors/devel/sig-architecture/api-conventions.md#resources",
                                "type": "string",
                            },
                            "kind": {
                                "description": "Kind is a string value representing the REST resource this object represents.\nServers may infer this from the endpoint the client submits requests to.\nCannot be updated.\nIn CamelCase.\nMore info: https://git.k8s.io/community/contributors/devel/sig-architecture/api-conventions.md#types-kinds",
                                "type": "string",
                            },
                            "metadata": {
                                "type": "object",
                            },
                            "spec": {
                                "description": 'InferenceModelSpec represents the desired state of a specific model use case. This resource is\nmanaged by the "Inference Workload Owner" persona.\n\nThe Inference Workload Owner persona is someone that trains, verifies, and\nleverages a large language model from a model frontend, drives the lifecycle\nand rollout of new versions of those models, and defines the specific\nperformance and latency goals for the model. These workloads are\nexpected to operate within an InferencePool sharing compute capacity with other\nInferenceModels, defined by the Inference Platform Admin.\n\nInferenceModel\'s modelName (not the ObjectMeta name) is unique for a given InferencePool,\nif the name is reused, an error will be shown on the status of a\nInferenceModel that attempted to reuse. The oldest InferenceModel, based on\ncreation timestamp, will be selected to remain valid. In the event of a race\ncondition, one will be selected at random.',
                                "properties": {
                                    "criticality": {
                                        "description": "Criticality defines how important it is to serve the model compared to other models referencing the same pool.\nCriticality impacts how traffic is handled in resource constrained situations. It handles this by\nqueuing or rejecting requests of lower criticality. InferenceModels of an equivalent Criticality will\nfairly share resources over throughput of tokens. In the future, the metric used to calculate fairness,\nand the proportionality of fairness will be configurable.\n\nDefault values for this field will not be set, to allow for future additions of new field that may 'one of' with this field.\nAny implementations that may consume this field may treat an unset value as the 'Standard' range.",
                                        "enum": [
                                            "Critical",
                                            "Standard",
                                            "Sheddable",
                                        ],
                                        "type": "string",
                                    },
                                    "modelName": {
                                        "description": 'ModelName is the name of the model as it will be set in the "model" parameter for an incoming request.\nModelNames must be unique for a referencing InferencePool\n(names can be reused for a different pool in the same cluster).\nThe modelName with the oldest creation timestamp is retained, and the incoming\nInferenceModel is sets the Ready status to false with a corresponding reason.\nIn the rare case of a race condition, one Model will be selected randomly to be considered valid, and the other rejected.\nNames can be reserved without an underlying model configured in the pool.\nThis can be done by specifying a target model and setting the weight to zero,\nan error will be returned specifying that no valid target model is found.',
                                        "maxLength": 256.0,
                                        "type": "string",
                                        "x-kubernetes-validations": [
                                            {
                                                "message": "modelName is immutable",
                                                "rule": "self == oldSelf",
                                            },
                                        ],
                                    },
                                    "poolRef": {
                                        "description": "PoolRef is a reference to the inference pool, the pool must exist in the same namespace.",
                                        "properties": {
                                            "group": {
                                                "default": "inference.networking.x-k8s.io",
                                                "description": "Group is the group of the referent.",
                                                "maxLength": 253.0,
                                                "pattern": "^$|^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$",
                                                "type": "string",
                                            },
                                            "kind": {
                                                "default": "InferencePool",
                                                "description": 'Kind is kind of the referent. For example "InferencePool".',
                                                "maxLength": 63.0,
                                                "minLength": 1.0,
                                                "pattern": "^[a-zA-Z]([-a-zA-Z0-9]*[a-zA-Z0-9])?$",
                                                "type": "string",
                                            },
                                            "name": {
                                                "description": "Name is the name of the referent.",
                                                "maxLength": 253.0,
                                                "minLength": 1.0,
                                                "type": "string",
                                            },
                                        },
                                        "required": [
                                            "name",
                                        ],
                                        "type": "object",
                                    },
                                    "targetModels": {
                                        "description": "TargetModels allow multiple versions of a model for traffic splitting.\nIf not specified, the target model name is defaulted to the modelName parameter.\nmodelName is often in reference to a LoRA adapter.",
                                        "items": {
                                            "description": "TargetModel represents a deployed model or a LoRA adapter. The\nName field is expected to match the name of the LoRA adapter\n(or base model) as it is registered within the model server. Inference\nGateway assumes that the model exists on the model server and it's the\nresponsibility of the user to validate a correct match. Should a model fail\nto exist at request time, the error is processed by the Inference Gateway\nand emitted on the appropriate InferenceModel object.",
                                            "properties": {
                                                "name": {
                                                    "description": "Name is the name of the adapter or base model, as expected by the ModelServer.",
                                                    "maxLength": 253.0,
                                                    "type": "string",
                                                },
                                                "weight": {
                                                    "description": "Weight is used to determine the proportion of traffic that should be\nsent to this model when multiple target models are specified.\n\nWeight defines the proportion of requests forwarded to the specified\nmodel. This is computed as weight/(sum of all weights in this\nTargetModels list). For non-zero values, there may be some epsilon from\nthe exact proportion defined here depending on the precision an\nimplementation supports. Weight is not a percentage and the sum of\nweights does not need to equal 100.\n\nIf a weight is set for any targetModel, it must be set for all targetModels.\nConversely weights are optional, so long as ALL targetModels do not specify a weight.",
                                                    "format": "int32",
                                                    "maximum": 1000000.0,
                                                    "minimum": 1.0,
                                                    "type": "integer",
                                                },
                                            },
                                            "required": [
                                                "name",
                                            ],
                                            "type": "object",
                                        },
                                        "maxItems": 10.0,
                                        "type": "array",
                                        "x-kubernetes-validations": [
                                            {
                                                "message": "Weights should be set for all models, or none of the models.",
                                                "rule": "self.all(model, has(model.weight)) || self.all(model, !has(model.weight))",
                                            },
                                        ],
                                    },
                                },
                                "required": [
                                    "modelName",
                                    "poolRef",
                                ],
                                "type": "object",
                            },
                            "status": {
                                "description": "InferenceModelStatus defines the observed state of InferenceModel",
                                "properties": {
                                    "conditions": {
                                        "default": [
                                            {
                                                "lastTransitionTime": "1970-01-01T00:00:00Z",
                                                "message": "Waiting for controller",
                                                "reason": "Pending",
                                                "status": "Unknown",
                                                "type": "Ready",
                                            },
                                        ],
                                        "description": 'Conditions track the state of the InferenceModel.\n\nKnown condition types are:\n\n* "Accepted"',
                                        "items": {
                                            "description": "Condition contains details for one aspect of the current state of this API Resource.",
                                            "properties": {
                                                "lastTransitionTime": {
                                                    "description": "lastTransitionTime is the last time the condition transitioned from one status to another.\nThis should be when the underlying condition changed.  If that is not known, then using the time when the API field changed is acceptable.",
                                                    "format": "date-time",
                                                    "type": "string",
                                                },
                                                "message": {
                                                    "description": "message is a human readable message indicating details about the transition.\nThis may be an empty string.",
                                                    "maxLength": 32768.0,
                                                    "type": "string",
                                                },
                                                "observedGeneration": {
                                                    "description": "observedGeneration represents the .metadata.generation that the condition was set based upon.\nFor instance, if .metadata.generation is currently 12, but the .status.conditions[x].observedGeneration is 9, the condition is out of date\nwith respect to the current state of the instance.",
                                                    "format": "int64",
                                                    "minimum": 0.0,
                                                    "type": "integer",
                                                },
                                                "reason": {
                                                    "description": "reason contains a programmatic identifier indicating the reason for the condition's last transition.\nProducers of specific condition types may define expected values and meanings for this field,\nand whether the values are considered a guaranteed API.\nThe value should be a CamelCase string.\nThis field may not be empty.",
                                                    "maxLength": 1024.0,
                                                    "minLength": 1.0,
                                                    "pattern": "^[A-Za-z]([A-Za-z0-9_,:]*[A-Za-z0-9_])?$",
                                                    "type": "string",
                                                },
                                                "status": {
                                                    "description": "status of the condition, one of True, False, Unknown.",
                                                    "enum": [
                                                        "True",
                                                        "False",
                                                        "Unknown",
                                                    ],
                                                    "type": "string",
                                                },
                                                "type": {
                                                    "description": "type of condition in CamelCase or in foo.example.com/CamelCase.",
                                                    "maxLength": 316.0,
                                                    "pattern": "^([a-z0-9]([-a-z0-9]*[a-z0-9])?(\\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*/)?(([A-Za-z0-9][-A-Za-z0-9_.]*)?[A-Za-z0-9])$",
                                                    "type": "string",
                                                },
                                            },
                                            "required": [
                                                "lastTransitionTime",
                                                "message",
                                                "reason",
                                                "status",
                                                "type",
                                            ],
                                            "type": "object",
                                        },
                                        "maxItems": 8.0,
                                        "type": "array",
                                        "x-kubernetes-list-map-keys": [
                                            "type",
                                        ],
                                        "x-kubernetes-list-type": "map",
                                    },
                                },
                                "type": "object",
                            },
                        },
                        "type": "object",
                    },
                },
                "served": True,
                "storage": True,
                "subresources": {
                    "status": {},
                },
            },
        ],
    },
}

# The InferencePool CRD manifest, used in cases 2 and 3.
_INFERENCE_POOL_CRD = {
    "apiVersion": "apiextensions.k8s.io/v1",
    "kind": "CustomResourceDefinition",
    "metadata": {
        "annotations": {
            "controller-gen.kubebuilder.io/version": "v0.16.1",
        },
        "name": "inferencepools.inference.networking.x-k8s.io",
    },
    "spec": {
        "group": "inference.networking.x-k8s.io",
        "names": {
            "kind": "InferencePool",
            "listKind": "InferencePoolList",
            "plural": "inferencepools",
            "singular": "inferencepool",
        },
        "scope": "Namespaced",
        "versions": [
            {
                "name": "v1alpha2",
                "schema": {
                    "openAPIV3Schema": {
                        "description": "InferencePool is the Schema for the InferencePools API.",
                        "properties": {
                            "apiVersion": {
                                "description": "APIVersion defines the versioned schema of this representation of an object.\nServers should convert recognized schemas to the latest internal value, and\nmay reject unrecognized values.\nMore info: https://git.k8s.io/community/contributors/devel/sig-architecture/api-conventions.md#resources",
                                "type": "string",
                            },
                            "kind": {
                                "description": "Kind is a string value representing the REST resource this object represents.\nServers may infer this from the endpoint the client submits requests to.\nCannot be updated.\nIn CamelCase.\nMore info: https://git.k8s.io/community/contributors/devel/sig-architecture/api-conventions.md#types-kinds",
                                "type": "string",
                            },
                            "metadata": {
                                "type": "object",
                            },
                            "spec": {
                                "description": "InferencePoolSpec defines the desired state of InferencePool",
                                "properties": {
                                    "extensionRef": {
                                        "description": "Extension configures an endpoint picker as an extension service.",
                                        "properties": {
                                            "failureMode": {
                                                "default": "FailClose",
                                                "description": "Configures how the gateway handles the case when the extension is not responsive.\nDefaults to failClose.",
                                                "enum": [
                                                    "FailOpen",
                                                    "FailClose",
                                                ],
                                                "type": "string",
                                            },
                                            "group": {
                                                "default": "",
                                                "description": 'Group is the group of the referent.\nThe default value is "", representing the Core API group.',
                                                "maxLength": 253.0,
                                                "pattern": "^$|^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$",
                                                "type": "string",
                                            },
                                            "kind": {
                                                "default": "Service",
                                                "description": 'Kind is the Kubernetes resource kind of the referent. For example\n"Service".\n\nDefaults to "Service" when not specified.\n\nExternalName services can refer to CNAME DNS records that may live\noutside of the cluster and as such are difficult to reason about in\nterms of conformance. They also may not be safe to forward to (see\nCVE-2021-25740 for more information). Implementations MUST NOT\nsupport ExternalName Services.',
                                                "maxLength": 63.0,
                                                "minLength": 1.0,
                                                "pattern": "^[a-zA-Z]([-a-zA-Z0-9]*[a-zA-Z0-9])?$",
                                                "type": "string",
                                            },
                                            "name": {
                                                "description": "Name is the name of the referent.",
                                                "maxLength": 253.0,
                                                "minLength": 1.0,
                                                "type": "string",
                                            },
                                            "portNumber": {
                                                "description": "The port number on the service running the extension. When unspecified,\nimplementations SHOULD infer a default value of 9002 when the Kind is\nService.",
                                                "format": "int32",
                                                "maximum": 65535.0,
                                                "minimum": 1.0,
                                                "type": "integer",
                                            },
                                        },
                                        "required": [
                                            "name",
                                        ],
                                        "type": "object",
                                    },
                                    "selector": {
                                        "additionalProperties": {
                                            "description": "LabelValue is the value of a label. This is used for validation\nof maps. This matches the Kubernetes label validation rules:\n* must be 63 characters or less (can be empty),\n* unless empty, must begin and end with an alphanumeric character ([a-z0-9A-Z]),\n* could contain dashes (-), underscores (_), dots (.), and alphanumerics between.\n\nValid values include:\n\n* MyValue\n* my.name\n* 123-my-value",
                                            "maxLength": 63.0,
                                            "minLength": 0.0,
                                            "pattern": "^(([A-Za-z0-9][-A-Za-z0-9_.]*)?[A-Za-z0-9])?$",
                                            "type": "string",
                                        },
                                        "description": "Selector defines a map of labels to watch model server pods\nthat should be included in the InferencePool.\nIn some cases, implementations may translate this field to a Service selector, so this matches the simple\nmap used for Service selectors instead of the full Kubernetes LabelSelector type.\nIf sepecified, it will be applied to match the model server pods in the same namespace as the InferencePool.\nCross namesoace selector is not supported.",
                                        "type": "object",
                                    },
                                    "targetPortNumber": {
                                        "description": "TargetPortNumber defines the port number to access the selected model servers.\nThe number must be in the range 1 to 65535.",
                                        "format": "int32",
                                        "maximum": 65535.0,
                                        "minimum": 1.0,
                                        "type": "integer",
                                    },
                                },
                                "required": [
                                    "extensionRef",
                                    "selector",
                                    "targetPortNumber",
                                ],
                                "type": "object",
                            },
                            "status": {
                                "description": "InferencePoolStatus defines the observed state of InferencePool",
                                "properties": {
                                    "parent": {
                                        "description": "Parents is a list of parent resources (usually Gateways) that are\nassociated with the route, and the status of the InferencePool with respect to\neach parent.\n\nA maximum of 32 Gateways will be represented in this list. An empty list\nmeans the route has not been attached to any Gateway.",
                                        "items": {
                                            "description": "PoolStatus defines the observed state of InferencePool from a Gateway.",
                                            "properties": {
                                                "conditions": {
                                                    "default": [
                                                        {
                                                            "lastTransitionTime": "1970-01-01T00:00:00Z",
                                                            "message": "Waiting for controller",
                                                            "reason": "Pending",
                                                            "status": "Unknown",
                                                            "type": "Accepted",
                                                        },
                                                    ],
                                                    "description": 'Conditions track the state of the InferencePool.\n\nKnown condition types are:\n\n* "Accepted"\n* "ResolvedRefs"',
                                                    "items": {
                                                        "description": "Condition contains details for one aspect of the current state of this API Resource.",
                                                        "properties": {
                                                            "lastTransitionTime": {
                                                                "description": "lastTransitionTime is the last time the condition transitioned from one status to another.\nThis should be when the underlying condition changed.  If that is not known, then using the time when the API field changed is acceptable.",
                                                                "format": "date-time",
                                                                "type": "string",
                                                            },
                                                            "message": {
                                                                "description": "message is a human readable message indicating details about the transition.\nThis may be an empty string.",
                                                                "maxLength": 32768.0,
                                                                "type": "string",
                                                            },
                                                            "observedGeneration": {
                                                                "description": "observedGeneration represents the .metadata.generation that the condition was set based upon.\nFor instance, if .metadata.generation is currently 12, but the .status.conditions[x].observedGeneration is 9, the condition is out of date\nwith respect to the current state of the instance.",
                                                                "format": "int64",
                                                                "minimum": 0.0,
                                                                "type": "integer",
                                                            },
                                                            "reason": {
                                                                "description": "reason contains a programmatic identifier indicating the reason for the condition's last transition.\nProducers of specific condition types may define expected values and meanings for this field,\nand whether the values are considered a guaranteed API.\nThe value should be a CamelCase string.\nThis field may not be empty.",
                                                                "maxLength": 1024.0,
                                                                "minLength": 1.0,
                                                                "pattern": "^[A-Za-z]([A-Za-z0-9_,:]*[A-Za-z0-9_])?$",
                                                                "type": "string",
                                                            },
                                                            "status": {
                                                                "description": "status of the condition, one of True, False, Unknown.",
                                                                "enum": [
                                                                    "True",
                                                                    "False",
                                                                    "Unknown",
                                                                ],
                                                                "type": "string",
                                                            },
                                                            "type": {
                                                                "description": "type of condition in CamelCase or in foo.example.com/CamelCase.",
                                                                "maxLength": 316.0,
                                                                "pattern": "^([a-z0-9]([-a-z0-9]*[a-z0-9])?(\\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*/)?(([A-Za-z0-9][-A-Za-z0-9_.]*)?[A-Za-z0-9])$",
                                                                "type": "string",
                                                            },
                                                        },
                                                        "required": [
                                                            "lastTransitionTime",
                                                            "message",
                                                            "reason",
                                                            "status",
                                                            "type",
                                                        ],
                                                        "type": "object",
                                                    },
                                                    "maxItems": 8.0,
                                                    "type": "array",
                                                    "x-kubernetes-list-map-keys": [
                                                        "type",
                                                    ],
                                                    "x-kubernetes-list-type": "map",
                                                },
                                                "parentRef": {
                                                    "description": "GatewayRef indicates the gateway that observed state of InferencePool.",
                                                    "properties": {
                                                        "apiVersion": {
                                                            "description": "API version of the referent.",
                                                            "type": "string",
                                                        },
                                                        "fieldPath": {
                                                            "description": 'If referring to a piece of an object instead of an entire object, this string\nshould contain a valid JSON/Go field access statement, such as desiredState.manifest.containers[2].\nFor example, if the object reference is to a container within a pod, this would take on a value like:\n"spec.containers{name}" (where "name" refers to the name of the container that triggered\nthe event) or if no container name is specified "spec.containers[2]" (container with\nindex 2 in this pod). This syntax is chosen only to have some well-defined way of\nreferencing a part of an object.',
                                                            "type": "string",
                                                        },
                                                        "kind": {
                                                            "description": "Kind of the referent.\nMore info: https://git.k8s.io/community/contributors/devel/sig-architecture/api-conventions.md#types-kinds",
                                                            "type": "string",
                                                        },
                                                        "name": {
                                                            "description": "Name of the referent.\nMore info: https://kubernetes.io/docs/concepts/overview/working-with-objects/names/#names",
                                                            "type": "string",
                                                        },
                                                        "namespace": {
                                                            "description": "Namespace of the referent.\nMore info: https://kubernetes.io/docs/concepts/overview/working-with-objects/namespaces/",
                                                            "type": "string",
                                                        },
                                                        "resourceVersion": {
                                                            "description": "Specific resourceVersion to which this reference is made, if any.\nMore info: https://git.k8s.io/community/contributors/devel/sig-architecture/api-conventions.md#concurrency-control-and-consistency",
                                                            "type": "string",
                                                        },
                                                        "uid": {
                                                            "description": "UID of the referent.\nMore info: https://kubernetes.io/docs/concepts/overview/working-with-objects/names/#uids",
                                                            "type": "string",
                                                        },
                                                    },
                                                    "type": "object",
                                                    "x-kubernetes-map-type": "atomic",
                                                },
                                            },
                                            "required": [
                                                "parentRef",
                                            ],
                                            "type": "object",
                                        },
                                        "maxItems": 32.0,
                                        "type": "array",
                                    },
                                },
                                "type": "object",
                            },
                        },
                        "type": "object",
                    },
                },
                "served": True,
                "storage": True,
                "subresources": {
                    "status": {},
                },
            },
        ],
    },
}

# Shared resource dicts used across test cases.
_PROVIDER_CONFIG_KUBERNETES = {
    "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
    "kind": "ProviderConfig",
    "metadata": {"name": _PC_NAME},
    "spec": {
        "credentials": {
            "secretRef": {
                "key": "kubeconfig",
                "name": "kube-secret",
                "namespace": "test-ns",
            },
            "source": "Secret",
        },
        "identity": {
            "secretRef": {
                "key": "private_key",
                "name": "sa-secret",
                "namespace": "test-ns",
            },
            "source": "Secret",
            "type": "GoogleApplicationCredentials",
        },
    },
}

_PROVIDER_CONFIG_HELM = {
    "apiVersion": "helm.m.crossplane.io/v1beta1",
    "kind": "ProviderConfig",
    "metadata": {"name": _PC_NAME},
    "spec": {
        "credentials": {
            "secretRef": {
                "key": "kubeconfig",
                "name": "kube-secret",
                "namespace": "test-ns",
            },
            "source": "Secret",
        },
        "identity": {
            "secretRef": {
                "key": "private_key",
                "name": "sa-secret",
                "namespace": "test-ns",
            },
            "source": "Secret",
            "type": "GoogleApplicationCredentials",
        },
    },
}

_USAGE_HELM_PC = {
    "apiVersion": "protection.crossplane.io/v1beta1",
    "kind": "Usage",
    "spec": {
        "by": {
            "apiVersion": "helm.m.crossplane.io/v1beta1",
            "kind": "Release",
            "resourceSelector": {"matchControllerRef": True},
        },
        "of": {
            "apiVersion": "helm.m.crossplane.io/v1beta1",
            "kind": "ProviderConfig",
            "resourceRef": {"name": _PC_NAME},
        },
        "replayDeletion": True,
    },
}

_USAGE_K8S_PC = {
    "apiVersion": "protection.crossplane.io/v1beta1",
    "kind": "Usage",
    "spec": {
        "by": {
            "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
            "kind": "Object",
            "resourceSelector": {"matchControllerRef": True},
        },
        "of": {
            "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
            "kind": "ProviderConfig",
            "resourceRef": {"name": _PC_NAME},
        },
        "replayDeletion": True,
    },
}

_USAGE_ENVOY_GW_BY_GATEWAY_CLASS = {
    "apiVersion": "protection.crossplane.io/v1beta1",
    "kind": "Usage",
    "spec": {
        "by": {
            "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
            "kind": "Object",
            "resourceSelector": {
                "matchControllerRef": True,
                "matchLabels": {"modelplane.ai/resource": "gateway-class"},
            },
        },
        "of": {
            "apiVersion": "helm.m.crossplane.io/v1beta1",
            "kind": "Release",
            "resourceSelector": {
                "matchControllerRef": True,
                "matchLabels": {"modelplane.ai/resource": "envoy-gateway"},
            },
        },
        "replayDeletion": True,
    },
}

_USAGE_GATEWAY_CLASS_BY_GATEWAY = {
    "apiVersion": "protection.crossplane.io/v1beta1",
    "kind": "Usage",
    "spec": {
        "by": {
            "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
            "kind": "Object",
            "resourceSelector": {
                "matchControllerRef": True,
                "matchLabels": {"modelplane.ai/resource": "gateway"},
            },
        },
        "of": {
            "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
            "kind": "Object",
            "resourceSelector": {
                "matchControllerRef": True,
                "matchLabels": {"modelplane.ai/resource": "gateway-class"},
            },
        },
        "replayDeletion": True,
    },
}

_USAGE_KSERVE_CRDS_BY_CONTROLLER = {
    "apiVersion": "protection.crossplane.io/v1beta1",
    "kind": "Usage",
    "spec": {
        "by": {
            "apiVersion": "helm.m.crossplane.io/v1beta1",
            "kind": "Release",
            "resourceSelector": {
                "matchControllerRef": True,
                "matchLabels": {"modelplane.ai/resource": "kserve-controller"},
            },
        },
        "of": {
            "apiVersion": "helm.m.crossplane.io/v1beta1",
            "kind": "Release",
            "resourceSelector": {
                "matchControllerRef": True,
                "matchLabels": {"modelplane.ai/resource": "kserve-crds"},
            },
        },
        "replayDeletion": True,
    },
}

_KSERVE_STORAGE_PATCH = {
    "apiVersion": "v1",
    "kind": "ConfigMap",
    "metadata": {
        "name": _STORAGE_PATCH_NAME,
        "namespace": "test-ns",
    },
    "data": {"patches": _KUSTOMIZE_STORAGE_PATCH},
}

_CERT_MANAGER = {
    "apiVersion": "helm.m.crossplane.io/v1beta1",
    "kind": "Release",
    "spec": {
        "forProvider": {
            "chart": {
                "name": "cert-manager",
                "repository": "https://charts.jetstack.io",
                "version": "v1.17.1",
            },
            "namespace": "cert-manager",
            "values": {
                "crds": {
                    "enabled": True,
                    "keep": False,
                },
            },
        },
        "providerConfigRef": {
            "kind": "ProviderConfig",
            "name": _PC_NAME,
        },
    },
}

_ENVOY_GATEWAY = {
    "apiVersion": "helm.m.crossplane.io/v1beta1",
    "kind": "Release",
    "metadata": {
        "labels": {"modelplane.ai/resource": "envoy-gateway"},
    },
    "spec": {
        "forProvider": {
            "chart": {
                "name": "gateway-helm",
                "repository": "oci://docker.io/envoyproxy",
                "version": "v1.3.0",
            },
            "namespace": "envoy-gateway-system",
            "values": {
                "config": {
                    "envoyGateway": {
                        "extensionApis": {"enableBackend": True},
                    },
                },
            },
        },
        "providerConfigRef": {
            "kind": "ProviderConfig",
            "name": _PC_NAME,
        },
    },
}

_GATEWAY = {
    "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
    "kind": "Object",
    "metadata": {
        "labels": {"modelplane.ai/resource": "gateway"},
    },
    "spec": {
        "forProvider": {
            "manifest": {
                "apiVersion": "gateway.networking.k8s.io/v1",
                "kind": "Gateway",
                "metadata": {
                    "name": "kserve-ingress-gateway",
                    "namespace": "kserve",
                },
                "spec": {
                    "gatewayClassName": "envoy",
                    "listeners": [
                        {
                            "allowedRoutes": {
                                "namespaces": {"from": "All"},
                            },
                            "name": "http",
                            "port": 80.0,
                            "protocol": "HTTP",
                        },
                    ],
                },
            },
        },
        "providerConfigRef": {
            "kind": "ProviderConfig",
            "name": _PC_NAME,
        },
    },
}

_GATEWAY_CLASS = {
    "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
    "kind": "Object",
    "metadata": {
        "labels": {"modelplane.ai/resource": "gateway-class"},
    },
    "spec": {
        "forProvider": {
            "manifest": {
                "apiVersion": "gateway.networking.k8s.io/v1",
                "kind": "GatewayClass",
                "metadata": {"name": "envoy"},
                "spec": {
                    "controllerName": "gateway.envoyproxy.io/gatewayclass-controller",
                },
            },
        },
        "providerConfigRef": {
            "kind": "ProviderConfig",
            "name": _PC_NAME,
        },
    },
}

_INFERENCE_EXT_CRD_INFERENCEMODELS = {
    "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
    "kind": "Object",
    "spec": {
        "forProvider": {
            "manifest": _INFERENCE_MODEL_CRD,
        },
        "providerConfigRef": {
            "kind": "ProviderConfig",
            "name": _PC_NAME,
        },
    },
}

_INFERENCE_EXT_CRD_INFERENCEPOOLS = {
    "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
    "kind": "Object",
    "spec": {
        "forProvider": {
            "manifest": _INFERENCE_POOL_CRD,
        },
        "providerConfigRef": {
            "kind": "ProviderConfig",
            "name": _PC_NAME,
        },
    },
}

_LEADER_WORKER_SET = {
    "apiVersion": "helm.m.crossplane.io/v1beta1",
    "kind": "Release",
    "spec": {
        "forProvider": {
            "chart": {
                "name": "lws",
                "repository": "oci://registry.k8s.io/lws/charts",
                "version": "v0.7.0",
            },
            "namespace": "lws-system",
        },
        "providerConfigRef": {
            "kind": "ProviderConfig",
            "name": _PC_NAME,
        },
    },
}

_PROMETHEUS = {
    "apiVersion": "helm.m.crossplane.io/v1beta1",
    "kind": "Release",
    "spec": {
        "forProvider": {
            "chart": {
                "name": "kube-prometheus-stack",
                "repository": "https://prometheus-community.github.io/helm-charts",
                "version": "72.6.2",
            },
            "namespace": "monitoring",
            "values": {
                "alertmanager": {"enabled": False},
                "fullnameOverride": "prometheus",
                "grafana": {"enabled": False},
                "prometheus": {
                    "prometheusSpec": {
                        "additionalScrapeConfigs": [
                            {
                                "job_name": "envoy-gateway-proxy",
                                "kubernetes_sd_configs": [
                                    {
                                        "namespaces": {
                                            "names": ["envoy-gateway-system"],
                                        },
                                        "role": "pod",
                                    },
                                ],
                                "metrics_path": "/stats/prometheus",
                                "relabel_configs": [
                                    {
                                        "action": "keep",
                                        "regex": "proxy",
                                        "source_labels": [
                                            "__meta_kubernetes_pod_label_app_kubernetes_io_component",
                                        ],
                                    },
                                    {
                                        "action": "replace",
                                        "regex": "([^:]+)(?::\\d+)?",
                                        "replacement": "$1:19001",
                                        "source_labels": ["__address__"],
                                        "target_label": "__address__",
                                    },
                                ],
                            },
                        ],
                        "podMonitorNamespaceSelector": {},
                        "podMonitorSelectorNilUsesHelmValues": False,
                    },
                },
            },
        },
        "providerConfigRef": {
            "kind": "ProviderConfig",
            "name": _PC_NAME,
        },
    },
}

_KSERVE_CRDS = {
    "apiVersion": "helm.m.crossplane.io/v1beta1",
    "kind": "Release",
    "metadata": {"labels": {"modelplane.ai/resource": "kserve-crds"}},
    "spec": {
        "forProvider": {
            "chart": {
                "name": "kserve-llmisvc-crd",
                "repository": "oci://ghcr.io/kserve/charts",
                "version": "v0.16.0",
            },
            "namespace": "kserve",
        },
        "providerConfigRef": {
            "kind": "ProviderConfig",
            "name": _PC_NAME,
        },
    },
}

_KSERVE_CONTROLLER = {
    "apiVersion": "helm.m.crossplane.io/v1beta1",
    "kind": "Release",
    "metadata": {"labels": {"modelplane.ai/resource": "kserve-controller"}},
    "spec": {
        "forProvider": {
            "chart": {
                "name": "kserve-llmisvc-resources",
                "repository": "oci://ghcr.io/kserve/charts",
                "version": "v0.16.0",
            },
            "namespace": "kserve",
            "patchesFrom": [
                {
                    "configMapKeyRef": {
                        "key": "patches",
                        "name": _STORAGE_PATCH_NAME,
                    },
                },
            ],
        },
        "providerConfigRef": {
            "kind": "ProviderConfig",
            "name": _PC_NAME,
        },
    },
}

_KEDA = {
    "apiVersion": "helm.m.crossplane.io/v1beta1",
    "kind": "Release",
    "spec": {
        "forProvider": {
            "chart": {
                "name": "keda",
                "repository": "https://kedacore.github.io/charts",
                "version": "2.17.1",
            },
            "namespace": "keda",
        },
        "providerConfigRef": {
            "kind": "ProviderConfig",
            "name": _PC_NAME,
        },
    },
}


def _base_request() -> fnv1.RunFunctionRequest:
    """Build the base RunFunctionRequest used by all test cases."""
    return fnv1.RunFunctionRequest(
        observed=fnv1.State(
            composite=fnv1.Resource(
                resource=resource.dict_to_struct(
                    v1alpha1.KServeBackend(
                        metadata=metav1.ObjectMeta(
                            name="test-backend",
                            namespace="test-ns",
                        ),
                        spec=v1alpha1.Spec(
                            secrets=[
                                v1alpha1.Secret(type="Kubeconfig", name="kube-secret", key="kubeconfig"),
                                v1alpha1.Secret(type="GCPServiceAccountKey", name="sa-secret", key="private_key"),
                            ],
                        ),
                    ).model_dump(exclude_none=True, mode="json")
                ),
            ),
        ),
    )


class TestFunctionRunner(unittest.IsolatedAsyncioTestCase):
    """Tests for FunctionRunner.RunFunction."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.runner = fn.FunctionRunner()

    async def test_first_pass(self) -> None:
        """First pass composes provider configs, usages, and storage patch; releases gated."""
        req = _base_request()

        want = fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=fnv1.Resource(
                    resource=resource.dict_to_struct({"status": {}}),
                ),
                resources={
                    "kserve-storage-patch": fnv1.Resource(
                        resource=resource.dict_to_struct(_KSERVE_STORAGE_PATCH),
                        ready=fnv1.READY_TRUE,
                    ),
                    "provider-config-helm": fnv1.Resource(
                        resource=resource.dict_to_struct(_PROVIDER_CONFIG_HELM),
                        ready=fnv1.READY_TRUE,
                    ),
                    "provider-config-kubernetes": fnv1.Resource(
                        resource=resource.dict_to_struct(_PROVIDER_CONFIG_KUBERNETES),
                        ready=fnv1.READY_TRUE,
                    ),
                    "usage-envoy-gw-by-gateway-class": fnv1.Resource(
                        resource=resource.dict_to_struct(_USAGE_ENVOY_GW_BY_GATEWAY_CLASS),
                        ready=fnv1.READY_TRUE,
                    ),
                    "usage-gateway-class-by-gateway": fnv1.Resource(
                        resource=resource.dict_to_struct(_USAGE_GATEWAY_CLASS_BY_GATEWAY),
                        ready=fnv1.READY_TRUE,
                    ),
                    "usage-kserve-crds-by-controller": fnv1.Resource(
                        resource=resource.dict_to_struct(_USAGE_KSERVE_CRDS_BY_CONTROLLER),
                        ready=fnv1.READY_TRUE,
                    ),
                    "usage-helm-pc": fnv1.Resource(
                        resource=resource.dict_to_struct(_USAGE_HELM_PC),
                        ready=fnv1.READY_TRUE,
                    ),
                    "usage-k8s-pc": fnv1.Resource(
                        resource=resource.dict_to_struct(_USAGE_K8S_PC),
                        ready=fnv1.READY_TRUE,
                    ),
                },
            ),
            context=structpb.Struct(),
        )

        got = await self.runner.RunFunction(req, None)
        self.assertEqual(
            json_format.MessageToDict(want),
            json_format.MessageToDict(got),
            "-want, +got",
        )

    async def test_second_pass(self) -> None:
        """Observed PCs ungate Helm releases, CRD objects, and gateway objects."""
        req = _base_request()
        req.observed.resources["provider-config-helm"].CopyFrom(
            fnv1.Resource(
                resource=resource.dict_to_struct(
                    {"apiVersion": "helm.m.crossplane.io/v1beta1", "kind": "ProviderConfig"}
                ),
            ),
        )
        req.observed.resources["provider-config-kubernetes"].CopyFrom(
            fnv1.Resource(
                resource=resource.dict_to_struct(
                    {"apiVersion": "kubernetes.m.crossplane.io/v1alpha1", "kind": "ProviderConfig"}
                ),
            ),
        )

        want = fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=fnv1.Resource(
                    resource=resource.dict_to_struct({"status": {}}),
                ),
                resources={
                    "cert-manager": fnv1.Resource(
                        resource=resource.dict_to_struct(_CERT_MANAGER),
                    ),
                    "envoy-gateway": fnv1.Resource(
                        resource=resource.dict_to_struct(_ENVOY_GATEWAY),
                    ),
                    "gateway": fnv1.Resource(
                        resource=resource.dict_to_struct(_GATEWAY),
                    ),
                    "gateway-class": fnv1.Resource(
                        resource=resource.dict_to_struct(_GATEWAY_CLASS),
                    ),
                    "inference-ext-crd-inferencemodels": fnv1.Resource(
                        resource=resource.dict_to_struct(_INFERENCE_EXT_CRD_INFERENCEMODELS),
                    ),
                    "inference-ext-crd-inferencepools": fnv1.Resource(
                        resource=resource.dict_to_struct(_INFERENCE_EXT_CRD_INFERENCEPOOLS),
                    ),
                    "kserve-storage-patch": fnv1.Resource(
                        resource=resource.dict_to_struct(_KSERVE_STORAGE_PATCH),
                        ready=fnv1.READY_TRUE,
                    ),
                    "leader-worker-set": fnv1.Resource(
                        resource=resource.dict_to_struct(_LEADER_WORKER_SET),
                    ),
                    "prometheus": fnv1.Resource(
                        resource=resource.dict_to_struct(_PROMETHEUS),
                    ),
                    "provider-config-helm": fnv1.Resource(
                        resource=resource.dict_to_struct(_PROVIDER_CONFIG_HELM),
                        ready=fnv1.READY_TRUE,
                    ),
                    "provider-config-kubernetes": fnv1.Resource(
                        resource=resource.dict_to_struct(_PROVIDER_CONFIG_KUBERNETES),
                        ready=fnv1.READY_TRUE,
                    ),
                    "usage-envoy-gw-by-gateway-class": fnv1.Resource(
                        resource=resource.dict_to_struct(_USAGE_ENVOY_GW_BY_GATEWAY_CLASS),
                        ready=fnv1.READY_TRUE,
                    ),
                    "usage-gateway-class-by-gateway": fnv1.Resource(
                        resource=resource.dict_to_struct(_USAGE_GATEWAY_CLASS_BY_GATEWAY),
                        ready=fnv1.READY_TRUE,
                    ),
                    "usage-kserve-crds-by-controller": fnv1.Resource(
                        resource=resource.dict_to_struct(_USAGE_KSERVE_CRDS_BY_CONTROLLER),
                        ready=fnv1.READY_TRUE,
                    ),
                    "usage-helm-pc": fnv1.Resource(
                        resource=resource.dict_to_struct(_USAGE_HELM_PC),
                        ready=fnv1.READY_TRUE,
                    ),
                    "usage-k8s-pc": fnv1.Resource(
                        resource=resource.dict_to_struct(_USAGE_K8S_PC),
                        ready=fnv1.READY_TRUE,
                    ),
                },
            ),
            context=structpb.Struct(),
        )

        got = await self.runner.RunFunction(req, None)
        self.assertEqual(
            json_format.MessageToDict(want),
            json_format.MessageToDict(got),
            "-want, +got",
        )

    async def test_third_pass(self) -> None:
        """cert-manager ready ungates KServe CRDs, controller, and KEDA; gateway address surfaced."""
        req = _base_request()
        req.observed.resources["provider-config-helm"].CopyFrom(
            fnv1.Resource(
                resource=resource.dict_to_struct(
                    {"apiVersion": "helm.m.crossplane.io/v1beta1", "kind": "ProviderConfig"}
                ),
            ),
        )
        req.observed.resources["provider-config-kubernetes"].CopyFrom(
            fnv1.Resource(
                resource=resource.dict_to_struct(
                    {"apiVersion": "kubernetes.m.crossplane.io/v1alpha1", "kind": "ProviderConfig"}
                ),
            ),
        )
        req.observed.resources["cert-manager"].CopyFrom(
            fnv1.Resource(
                resource=resource.dict_to_struct(
                    {
                        "apiVersion": "helm.m.crossplane.io/v1beta1",
                        "kind": "Release",
                        "status": {"conditions": [{"type": "Ready", "status": "True"}]},
                    }
                ),
            ),
        )
        req.observed.resources["gateway"].CopyFrom(
            fnv1.Resource(
                resource=resource.dict_to_struct(
                    {
                        "apiVersion": "kubernetes.m.crossplane.io/v1alpha1",
                        "kind": "Object",
                        "status": {
                            "atProvider": {
                                "manifest": {"status": {"addresses": [{"value": "172.18.255.200"}]}},
                            },
                        },
                    }
                ),
            ),
        )

        want = fnv1.RunFunctionResponse(
            meta=fnv1.ResponseMeta(ttl=durationpb.Duration(seconds=60)),
            desired=fnv1.State(
                composite=fnv1.Resource(
                    resource=resource.dict_to_struct(
                        {"status": {"gateway": {"address": "172.18.255.200"}}},
                    ),
                ),
                resources={
                    "cert-manager": fnv1.Resource(
                        resource=resource.dict_to_struct(_CERT_MANAGER),
                        ready=fnv1.READY_TRUE,
                    ),
                    "envoy-gateway": fnv1.Resource(
                        resource=resource.dict_to_struct(_ENVOY_GATEWAY),
                    ),
                    "gateway": fnv1.Resource(
                        resource=resource.dict_to_struct(_GATEWAY),
                    ),
                    "gateway-class": fnv1.Resource(
                        resource=resource.dict_to_struct(_GATEWAY_CLASS),
                    ),
                    "inference-ext-crd-inferencemodels": fnv1.Resource(
                        resource=resource.dict_to_struct(_INFERENCE_EXT_CRD_INFERENCEMODELS),
                    ),
                    "inference-ext-crd-inferencepools": fnv1.Resource(
                        resource=resource.dict_to_struct(_INFERENCE_EXT_CRD_INFERENCEPOOLS),
                    ),
                    "keda": fnv1.Resource(
                        resource=resource.dict_to_struct(_KEDA),
                    ),
                    "kserve-controller": fnv1.Resource(
                        resource=resource.dict_to_struct(_KSERVE_CONTROLLER),
                    ),
                    "kserve-crds": fnv1.Resource(
                        resource=resource.dict_to_struct(_KSERVE_CRDS),
                    ),
                    "kserve-storage-patch": fnv1.Resource(
                        resource=resource.dict_to_struct(_KSERVE_STORAGE_PATCH),
                        ready=fnv1.READY_TRUE,
                    ),
                    "leader-worker-set": fnv1.Resource(
                        resource=resource.dict_to_struct(_LEADER_WORKER_SET),
                    ),
                    "prometheus": fnv1.Resource(
                        resource=resource.dict_to_struct(_PROMETHEUS),
                    ),
                    "provider-config-helm": fnv1.Resource(
                        resource=resource.dict_to_struct(_PROVIDER_CONFIG_HELM),
                        ready=fnv1.READY_TRUE,
                    ),
                    "provider-config-kubernetes": fnv1.Resource(
                        resource=resource.dict_to_struct(_PROVIDER_CONFIG_KUBERNETES),
                        ready=fnv1.READY_TRUE,
                    ),
                    "usage-envoy-gw-by-gateway-class": fnv1.Resource(
                        resource=resource.dict_to_struct(_USAGE_ENVOY_GW_BY_GATEWAY_CLASS),
                        ready=fnv1.READY_TRUE,
                    ),
                    "usage-gateway-class-by-gateway": fnv1.Resource(
                        resource=resource.dict_to_struct(_USAGE_GATEWAY_CLASS_BY_GATEWAY),
                        ready=fnv1.READY_TRUE,
                    ),
                    "usage-kserve-crds-by-controller": fnv1.Resource(
                        resource=resource.dict_to_struct(_USAGE_KSERVE_CRDS_BY_CONTROLLER),
                        ready=fnv1.READY_TRUE,
                    ),
                    "usage-helm-pc": fnv1.Resource(
                        resource=resource.dict_to_struct(_USAGE_HELM_PC),
                        ready=fnv1.READY_TRUE,
                    ),
                    "usage-k8s-pc": fnv1.Resource(
                        resource=resource.dict_to_struct(_USAGE_K8S_PC),
                        ready=fnv1.READY_TRUE,
                    ),
                },
            ),
            results=[
                fnv1.Result(
                    severity=fnv1.SEVERITY_NORMAL,
                    message="cert-manager ready, composing KServe",
                ),
            ],
            context=structpb.Struct(),
        )

        got = await self.runner.RunFunction(req, None)
        self.assertEqual(
            json_format.MessageToDict(want),
            json_format.MessageToDict(got),
            "-want, +got",
        )

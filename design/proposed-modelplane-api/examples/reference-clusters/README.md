# Reference InferenceClusters

Pre-generated `InferenceCluster` definitions for known cloud / on-prem
SKUs. Drawn from @bassam's hardware survey (2026-05-07). Customers copy
or compose these instead of authoring `nodePools[]` from scratch.

Each reference cluster declares attributes against the default
`CapabilityVocabulary` taxonomy (Cluster / Pool / Device layers,
capability sets, `cloud.instanceType` macros).

## Why these matter

Modelplane keeps these reference clusters current as new cloud SKUs ship
(B300, GB300, GH200 successors, MI400-class AMD, etc.). This is the
canonical-catalog work that anchors the managed-catalog commercial
offering — bounded, ongoing, high-leverage. We layer continuous
**testing and benchmarking** on top: each reference cluster is paired
with a tested-and-benchmarked workload run on every supported model
family so customers can rely on the catalog, not just consume YAML.

## What's here

| File | Cloud / system | Topology |
|---|---|---|
| `aws-p5-48xlarge.yaml` | AWS | 8× H100 SXM, EFA |
| `gke-a3-mega-8g.yaml` | GCP | 8× H100 SXM, RoCE |
| `oci-bm-gpu-mi300x-8.yaml` | OCI | 8× AMD MI300X, RoCE |
| `coreweave-gb300-nvl72.yaml` | CoreWeave | NVL72 rack-scale (B300) |

## Future direction

These are static artifacts today (low-effort, high-signal). The natural
next step is a Crossplane provider that polls cloud SKU APIs
(`gcloud compute machine-types list`, AWS `DescribeInstanceTypes`,
Azure equivalent) and generates these resources programmatically — so
operators don't keep labels up to date by hand. See the design doc's
"Hardware taxonomy & reference clusters" section.

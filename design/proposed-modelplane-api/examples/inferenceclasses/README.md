# InferenceClass catalog

Reusable hardware-class bundles, one per known SKU. StorageClass /
IngressClass / DeviceClass pattern. Modelplane ships a default set;
customers author their own for bespoke hardware.

`InferenceCluster.spec.nodePools[].class` references a class by name;
the matcher inherits `class.expands` into the pool's effective Pool +
Device attributes. `class.aliases[]` lets a per-cloud SKU string
(e.g. `aws:p5.48xlarge`) resolve to the canonical class.

This directory is the default catalog. New SKUs land here without a
Modelplane CRD bump — the same managed-catalog work that anchors the
commercial offering.

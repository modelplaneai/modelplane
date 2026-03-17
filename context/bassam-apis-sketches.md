# Modelplane API Design

**Version:** 1.0  
**Date:** January 2025  
**Status:** Design Document

## Overview

Modelplane is an open source AI inference orchestration platform that enables organizations to deploy and serve AI models across any infrastructure. Following Baseten's information model closely, Modelplane provides a Kubernetes-native control plane for managing inference deployments with enterprise-grade operations.

### Key Design Principles

**Kubernetes-native:** Uses namespaces for environment isolation, follows CRD patterns, integrates with native RBAC and resource quotas.

**Separation of concerns:** Platform engineers manage infrastructure and capabilities. Application developers deploy models.

**Baseten-aligned:** API closely matches Baseten's deployment model for familiar developer experience.

**XRD pattern for engines:** Engine definitions (cluster-scoped) define capabilities and schemas. Engine instances (namespaced) provide environment-specific configurations.

**Two-level policies:** Global cluster policies set platform-wide limits. Namespace policies provide team-specific constraints.

## Architecture

### Namespace as Environment

**Core concept:** Kubernetes namespace \= deployment environment

Each namespace represents an isolated environment (production, staging, team namespaces). Resources are scoped to namespaces for natural isolation using built-in Kubernetes primitives.

```
Namespace: production
├─ Environment (defines environment config)
├─ Cluster resources (production clusters)
├─ Engine instances (production engines)
├─ Gateway (production endpoint)
├─ Policy (production limits)
└─ Deployment resources (user models)

Namespace: staging
├─ Environment
├─ Cluster resources (staging clusters)
├─ Engine instances
├─ Gateway
├─ Policy
└─ Deployment resources
```

**Benefits:**

- Native Kubernetes isolation boundary  
- RBAC via namespace  
- ResourceQuota per namespace  
- No cross-namespace resource sharing  
- GitOps-friendly structure

### API Groups

**platform.modelplane.ai/v1alpha1** \- Infrastructure and platform configuration

**inference.modelplane.ai/v1alpha1** \- Model deployments and serving

## Resource Types

### Cluster-Scoped Resources

#### EngineDefinition

**Purpose:** Define engine capabilities and configuration schema (immutable definition)

**Who creates:** Platform team (once, globally)

**Example:**

```
apiVersion: platform.modelplane.ai/v1alpha1
kind: EngineDefinition
metadata:
  name: vllm
spec:
  engine: vllm
  versions:
    - name: "0.6.3"
      
      capabilities:
        supportedArchitectures:
          - llama
          - mistral
          - gemma
          - qwen
          - deepseek
        
        features:
          - prefix-caching
          - chunked-prefill
        
        quantization:
          - awq
          - gptq
          - fp8
      
      installation:
        type: helm
        helm:
          chart: oci://ghcr.io/modelplane/vllm
          version: "0.6.3"
      
      configSchema:
        openAPIV3Schema:
          type: object
          properties:
            gpu_memory_utilization:
              type: number
              minimum: 0.7
              maximum: 0.98
              default: 0.9
            
            max_model_len:
              type: integer
              minimum: 512
              maximum: 32768
              default: 4096
            
            tensor_parallel_size:
              type: integer
              minimum: 1
              maximum: 8
              default: 1
      
      defaultConfig:
        gpu_memory_utilization: 0.9
        max_model_len: 4096
```

**Key features:**

- OpenAPI schema for automatic validation  
- Multiple versions supported  
- Declares supported model architectures  
- Reusable across all environments

#### ClusterPolicy

**Purpose:** Platform-wide policies and limits

**Who creates:** Platform team

**Example:**

```
apiVersion: platform.modelplane.ai/v1alpha1
kind: ClusterPolicy
metadata:
  name: global-limits
spec:
  limits:
    maxDeploymentsTotal: 1000
    maxAcceleratorsTotal: 2048
    maxCostPerMonth: 5000000
  
  allowedEngineDefinitions:
    - vllm
    - tensorrt-llm
  
  perDeploymentLimits:
    maxAcceleratorsPerDeployment: 64
    maxReplicasPerDeployment: 100
  
  security:
    allowedModelSources:
      - huggingface
      - s3
      - gcs

status:
  currentUsage:
    totalDeployments: 145
    totalAccelerators: 896
    totalCostThisMonth: 1250000
  
  namespaces:
    - name: production
      deployments: 42
      accelerators: 512
      costThisMonth: 800000
```

### Namespaced Resources (Platform)

#### Environment

**Purpose:** Define environment-level configuration and metadata

**Who creates:** Platform engineer per namespace

**Example:**

```
apiVersion: platform.modelplane.ai/v1alpha1
kind: Environment
metadata:
  name: production-env
  namespace: production
spec:
  description: "Production inference environment"
  
  labels:
    environment: production
    cost-center: engineering
    team: ml-platform
  
  gateway:
    enabled: true
    domain: production.gateway.company.com
    tls:
      enabled: true
      certManager:
        issuerRef:
          name: letsencrypt-prod
    
    routing:
      strategy: intelligent
    
    rateLimiting:
      globalRequestsPerSecond: 10000
      perDeploymentRequestsPerSecond: 1000

status:
  phase: Ready
  
  clusters:
    total: 2
    ready: 2
    items:
      - name: aws-us-west-2
        phase: Ready
        accelerators:
          type: H100
          total: 64
          available: 48
      
      - name: aws-us-east-1
        phase: Ready
        accelerators:
          type: A100
          total: 32
          available: 20
  
  engines:
    total: 2
    items:
      - name: high-throughput
        engine: vllm
        version: "0.6.3"
        phase: Ready
      
      - name: low-latency
        engine: tensorrt-llm
        version: "0.13.0"
        phase: Ready
  
  gateway:
    phase: Active
    endpoint: https://production.gateway.company.com
    deployments: 12
```

**Key features:**

- Single Environment per namespace (enforced by admission webhook)  
- Aggregates cluster and engine status  
- Gateway configuration centralized  
- Environment-level metadata and labels

#### Cluster

**Purpose:** Kubernetes cluster with accelerators (namespaced, not shared)

**Who creates:** Platform engineer per namespace

**Example:**

```
apiVersion: platform.modelplane.ai/v1alpha1
kind: Cluster
metadata:
  name: aws-us-west-2
  namespace: production
spec:
  provider: aws
  region: us-west-2
  
  accelerator:
    type: gpu
    gpu:
      type: H100
      count: 64
  
  networking:
    vpcId: vpc-12345
    privateEndpoint: true
  
  labels:
    region: us-west-2
    cost-tier: standard

status:
  phase: Ready
  
  capacity:
    accelerator:
      gpu:
        type: H100
        total: 64
        available: 48
        allocated: 16
  
  usedBy:
    - deployment: fraud-detection
      accelerators: 8
    
    - deployment: customer-support
      accelerators: 8
```

**Key features:**

- Namespaced (not shared across environments)  
- Can provision via Crossplane  
- Reports real-time capacity  
- Tracks usage per deployment

#### Engine

**Purpose:** Engine instance with environment-specific configuration

**Who creates:** Platform engineer per namespace

**Example:**

```
apiVersion: platform.modelplane.ai/v1alpha1
kind: Engine
metadata:
  name: high-throughput
  namespace: production
spec:
  engineDefinitionRef:
    name: vllm
    version: "0.6.3"
  
  config:
    gpu_memory_utilization: 0.95
    max_model_len: 8192
    tensor_parallel_size: 8
  
  resources:
    accelerator:
      gpu:
        type: H100
        count: 8
    memory: 600Gi
    cpu: 64

status:
  phase: Ready
  
  engine: vllm
  version: "0.6.3"
  
  capabilities:
    supportedArchitectures:
      - llama
      - mistral
      - deepseek
  
  effectiveConfig:
    gpu_memory_utilization: 0.95
    max_model_len: 8192
    enable_prefix_caching: true
    tensor_parallel_size: 8
  
  availableOn:
    clusters:
      - aws-us-west-2
```

**Key features:**

- References cluster-scoped EngineDefinition  
- Configuration validated against EngineDefinition schema  
- Environment-specific resource requirements  
- Shows effective configuration (defaults \+ overrides)

#### Policy

**Purpose:** Namespace-specific policies and limits

**Who creates:** Platform engineer per namespace

**Example:**

```
apiVersion: platform.modelplane.ai/v1alpha1
kind: Policy
metadata:
  name: default
  namespace: production
spec:
  limits:
    maxDeployments: 100
    maxAccelerators: 512
    maxCostPerMonth: 1000000
  
  allowedEngines:
    - high-throughput
    - low-latency
  
  resourceRequirements:
    minimumAcceleratorsPerReplica: 1
    maximumAcceleratorsPerReplica: 16
  
  placement:
    allowedClusters:
      - aws-us-west-2
  
  security:
    requireModelValidation: true
    
    allowedModelSources:
      - s3
    
    compliance:
      dataResidency:
        allowedRegions:
          - us-west-2
          - us-east-1

status:
  currentUsage:
    deployments: 42
    accelerators: 512
    costThisMonth: 800000
```

**Key features:**

- More restrictive than ClusterPolicy (cannot be less)  
- Enforces namespace-specific limits  
- Placement and security policies  
- Real-time usage tracking

### Namespaced Resources (Inference)

#### Deployment

**Purpose:** Deploy and serve a model

**Who creates:** Application developer

**Example:**

```
apiVersion: inference.modelplane.ai/v1alpha1
kind: Deployment
metadata:
  name: fraud-detection
  namespace: production
spec:
  model:
    source: huggingface
    repository: meta-llama/Llama-3.1-70B-Instruct
    revision: main
  
  engine: high-throughput
  
  engineConfig:
    max_model_len: 8192
  
  resources:
    accelerator: H100
    acceleratorsPerReplica: 8
  
  autoscaling:
    minReplicas: 2
    maxReplicas: 10
    maxConcurrencyPerReplica: 100
    targetConcurrencyPerReplica: 80

status:
  phase: Active
  
  model:
    repository: meta-llama/Llama-3.1-70B-Instruct
    architecture: llama
    size: 140GB
  
  engine:
    name: high-throughput
    engineDefinition: vllm
    version: "0.6.3"
    compatible: true
  
  effectiveConfig:
    gpu_memory_utilization: 0.95
    max_model_len: 8192
    tensor_parallel_size: 8
  
  clusters:
    - name: aws-us-west-2
      replicas: 4
      ready: 4
      accelerators: 32
  
  replicas:
    current: 4
    ready: 4
    min: 2
    max: 10
  
  endpoint: https://production.gateway.company.com/v1/fraud-detection
  
  metrics:
    requestsPerSecond: 250
    avgLatencyMs: 120
    p99LatencyMs: 450
  
  cost:
    currentPerHour: 210.00
    estimatedPerDay: 5040.00
    estimatedPerMonth: 151200.00
```

**Key features:**

- Matches Baseten deployment API closely  
- Auto-detects model architecture  
- Validates against engine capabilities  
- Shows effective configuration  
- Provides cost estimates

#### Model

**Purpose:** Register custom models (optional)

**Who creates:** Application developer

**Example:**

```
apiVersion: inference.modelplane.ai/v1alpha1
kind: Model
metadata:
  name: custom-llama-70b
  namespace: production
spec:
  source: s3
  path: s3://ml-models/llama-70b-v2/
  
  architecture: llama
  
  metadata:
    baseModel: meta-llama/Llama-3.1-70B-Instruct
    version: "v2.0"
    description: "Fine-tuned on customer support data"
    validated: true
  
  cache:
    enabled: true
    preWarm:
      clusters:
        - aws-us-west-2

status:
  phase: Ready
  size: 142GB
  digest: sha256:abc123...
  
  usedBy:
    - deployment: fraud-detection
      replicas: 4
    
    - deployment: support-bot
      replicas: 2
  
  cached:
    - cluster: aws-us-west-2
      cached: true
      lastAccessed: "2025-01-26T12:00:00Z"
```

## Validation Flow

### Model Architecture Compatibility

**Step 1: User creates Deployment**

```
kind: Deployment
spec:
  model:
    source: huggingface
    repository: deepseek-ai/DeepSeek-V3
  engine: low-latency
```

**Step 2: Controller validates**

1. Get Engine instance from namespace  
2. Get EngineDefinition from Engine.spec.engineDefinitionRef  
3. Auto-detect model architecture (query HuggingFace API) → "deepseek"  
4. Check if "deepseek" in EngineDefinition.capabilities.supportedArchitectures  
5. If not supported, return error with helpful message

**Step 3: Status shows result**

```
status:
  phase: Failed
  conditions:
    - type: EngineCompatible
      status: "False"
      reason: UnsupportedArchitecture
      message: |
        Model 'deepseek-ai/DeepSeek-V3' uses architecture 'deepseek'
        which is not supported by engine 'low-latency' (tensorrt-llm 0.13.0).
        
        Supported architectures: llama, mistral, gemma, qwen2, phi-3
        
        Alternative engines that support 'deepseek':
        - high-throughput (vllm 0.6.3)
```

### Configuration Validation

Engine configuration validated against EngineDefinition.configSchema (OpenAPI):

```
# This gets rejected automatically
kind: Engine
spec:
  config:
    gpu_memory_utilization: 1.5  # Error: maximum is 0.98
    max_model_len: "invalid"      # Error: must be integer
```

Kubernetes API server validates using OpenAPI schema before persisting resource.

### Policy Validation

Controllers validate against ClusterPolicy and namespace Policy:

1. Check ClusterPolicy.allowedEngineDefinitions  
2. Check Policy.allowedEngines  
3. Check resource limits (per-deployment, namespace total, global total)  
4. Check placement constraints (allowed clusters, regions)  
5. Check security policies (model sources, validation requirements)

## User Workflows

### Platform Engineer: Setup Environment

```shell
# Step 1: Create EngineDefinitions (once, cluster-wide)
kubectl apply -f engine-definitions/vllm.yaml
kubectl apply -f engine-definitions/tensorrt-llm.yaml

# Step 2: Create ClusterPolicy
kubectl apply -f cluster-policy.yaml

# Step 3: Create namespace
kubectl create namespace production
kubectl label namespace production environment=production

# Step 4: Create Environment resource
kubectl apply -f - <<EOF
apiVersion: platform.modelplane.ai/v1alpha1
kind: Environment
metadata:
  name: production-env
  namespace: production
spec:
  description: "Production inference environment"
  gateway:
    enabled: true
    domain: production.gateway.company.com
EOF

# Step 5: Create Clusters in namespace
kubectl apply -f clusters/aws-us-west-2.yaml -n production
kubectl apply -f clusters/aws-us-east-1.yaml -n production

# Step 6: Create Engine instances in namespace
kubectl apply -f - <<EOF
apiVersion: platform.modelplane.ai/v1alpha1
kind: Engine
metadata:
  name: high-throughput
  namespace: production
spec:
  engineDefinitionRef:
    name: vllm
    version: "0.6.3"
  config:
    gpu_memory_utilization: 0.95
    tensor_parallel_size: 8
  resources:
    accelerator:
      gpu:
        type: H100
        count: 8
EOF

# Step 7: Create Policy
kubectl apply -f policy.yaml -n production

# Step 8: Check environment status
kubectl get environment -n production
```

### Application Developer: Deploy Model

```shell
# List available engines
kubectl get engine -n production

# Get engine details
kubectl describe engine high-throughput -n production

# Deploy model
kubectl apply -f - <<EOF
apiVersion: inference.modelplane.ai/v1alpha1
kind: Deployment
metadata:
  name: fraud-detection
  namespace: production
spec:
  model:
    source: huggingface
    repository: meta-llama/Llama-3.1-70B-Instruct
  
  engine: high-throughput
  
  engineConfig:
    max_model_len: 8192
  
  autoscaling:
    minReplicas: 2
    maxReplicas: 10
EOF

# Check deployment status
kubectl get deployment fraud-detection -n production

# Get endpoint
kubectl get deployment fraud-detection -n production \
  -o jsonpath='{.status.endpoint}'

# Use endpoint
curl https://production.gateway.company.com/v1/fraud-detection/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "messages": [{"role": "user", "content": "Hello"}]
  }'
```

## Gateway Architecture

### Gateway per Namespace

Each namespace has its own Gateway resource configured via Environment:

```
Namespace: production
├─ Environment (defines gateway config)
├─ Gateway pods (deployed in namespace)
│  └─ Routes to Deployments in namespace
└─ Deployments

Namespace: staging
├─ Environment (defines gateway config)
├─ Gateway pods (deployed in namespace)
└─ Deployments
```

### Request Flow

1. User → gateway.production.company.com  
2. DNS resolves to Gateway in production namespace  
3. Gateway routes to Deployment pods in production namespace  
4. Response returned through Gateway

### Gateway Configuration (via Environment)

```
kind: Environment
spec:
  gateway:
    enabled: true
    domain: production.gateway.company.com
    
    routing:
      strategy: intelligent  # or round-robin, least-connections
    
    rateLimiting:
      globalRequestsPerSecond: 10000
      perDeploymentRequestsPerSecond: 1000
```

Gateway controller watches Environment resource and configures Gateway accordingly.

## Benefits Summary

### Kubernetes-Native

Uses namespaces for isolation, follows CRD patterns, integrates with RBAC and ResourceQuota. No custom abstractions where Kubernetes primitives suffice.

### Clean Separation of Concerns

Platform engineers manage EngineDefinitions (capabilities), ClusterPolicy (global limits), and namespace resources (Cluster, Engine, Environment, Policy). Application developers create Deployments (models) without infrastructure knowledge.

### OpenAPI Schema Validation

EngineDefinition uses OpenAPI schema. Kubernetes API server validates Engine and Deployment configurations automatically. No custom validation logic needed.

### Two-Level Policies

ClusterPolicy sets platform-wide baseline. Namespace Policy can be more restrictive. Ensures security while allowing team autonomy.

### Baseten-Aligned API

Deployment resource closely matches Baseten's deployment API. Familiar experience for users coming from managed platforms. Easy mental model: model \+ engine \+ resources \+ autoscaling.

### XRD Pattern for Engines

EngineDefinition (cluster-scoped) defines capabilities and schema once. Engine (namespaced) provides environment-specific configuration. Proven pattern from Crossplane.

### No Cross-Namespace Sharing

Clusters are namespaced (not shared). Simpler mental model, clearer ownership, easier RBAC. Each environment is fully isolated.

## Resource Summary

### Cluster-Scoped (2)

**EngineDefinition** \- Engine capabilities and config schema (platform.modelplane.ai)

**ClusterPolicy** \- Global platform policies (platform.modelplane.ai)

### Namespaced Platform (5)

**Environment** \- Environment configuration and gateway (platform.modelplane.ai)

**Cluster** \- Kubernetes cluster with accelerators (platform.modelplane.ai)

**Engine** \- Engine instance with configuration (platform.modelplane.ai)

**Policy** \- Namespace policies and limits (platform.modelplane.ai)

**Gateway** \- (Created automatically by Environment controller)

### Namespaced Inference (2)

**Deployment** \- Model deployment for serving (inference.modelplane.ai)

**Model** \- Custom model registry, optional (inference.modelplane.ai)

## Next Steps

### Implementation Phases

**Phase 1: Core Platform**

- Implement EngineDefinition, Engine, Cluster CRDs  
- Basic validation (architecture compatibility)  
- Single-cluster deployments  
- Manual gateway configuration

**Phase 2: Environment & Gateway**

- Implement Environment resource  
- Automatic gateway configuration  
- Policy enforcement (ClusterPolicy, Policy)  
- Multi-cluster placement

**Phase 3: Intelligence Layer**

- Intelligent routing (GPU utilization, queue depth)  
- Cost optimization  
- Predictive autoscaling  
- Advanced observability

### Open Questions

**Engine versioning:** How to handle engine upgrades in-place vs new Engine resources?

**Model caching:** DaemonSet vs init containers for model downloads?

**Cross-cluster scheduling:** Algorithm for optimal cluster selection?

**Gateway implementation:** Envoy vs NGINX vs custom?  


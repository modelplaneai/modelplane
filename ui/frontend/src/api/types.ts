export interface ObjectMeta {
  name: string;
  namespace?: string;
  uid?: string;
  labels?: Record<string, string>;
  annotations?: Record<string, string>;
  creationTimestamp?: string;
}

export interface Condition {
  type: string;
  status: "True" | "False" | "Unknown";
  reason?: string;
  message?: string;
  lastTransitionTime?: string;
}

export interface KubeList<T> {
  items: T[];
  metadata: { resourceVersion?: string };
}

export interface GPUPool {
  acceleratorType: string;
  memory: string;
  // countPerNode is the number of GPUs on each node in this pool. Total GPU
  // count is countPerNode * nodes.
  countPerNode: number;
  // nodes is the number of nodes in this pool.
  nodes: number;
}

export interface InferenceGateway {
  apiVersion: "modelplane.ai/v1alpha1";
  kind: "InferenceGateway";
  metadata: ObjectMeta;
  spec: {
    backend: string;
    envoyGateway?: { version?: string; loadBalancer?: string };
    gateway?: { port?: number };
  };
  status?: {
    conditions?: Condition[];
    address?: string;
  };
}

export interface InferenceEnvironment {
  apiVersion: "modelplane.ai/v1alpha1";
  kind: "InferenceEnvironment";
  metadata: ObjectMeta;
  spec: {
    cluster?: {
      source: string;
      gke?: {
        project: string;
        region: string;
        nodePools?: Array<{
          name: string;
          role: string;
          machineType: string;
          nodeCount?: number;
          gpu?: {
            acceleratorType: string;
            acceleratorCount?: number;
            memory: string;
          };
        }>;
      };
      existing?: {
        nodePools?: Array<{
          name: string;
          nodeCount?: number;
          gpu?: {
            acceleratorType: string;
            acceleratorCount?: number;
            memory: string;
          };
        }>;
      };
    };
  };
  status?: {
    conditions?: Condition[];
    providerConfigRef?: { name: string };
    gateway?: { address: string };
    capacity?: { gpuPools?: GPUPool[] };
    namespace?: string;
  };
}

export interface ServingProfile {
  name: string;
  environmentSelector?: { matchLabels?: Record<string, string> };
  engine?: {
    name: string;
    image: string;
    args?: string[];
  };
}

export interface ClusterModel {
  apiVersion: "modelplane.ai/v1alpha1";
  kind: "ClusterModel";
  metadata: ObjectMeta;
  spec: {
    model: { name: string };
    source: string;
    huggingFace?: { repo: string; revision?: string };
    resources: { vram: string; cpu?: string; memory?: string };
    serving?: ServingProfile[];
  };
  status?: {
    conditions?: Condition[];
  };
}

export interface ScalingFixed {
  replicas: number;
}

export interface ScalingConcurrency {
  maxReplicas: number;
  target: number;
  minReplicas?: number;
  utilization?: number;
  scaleDownDelay?: number;
}

export interface Scaling {
  signal: "Fixed" | "Concurrency";
  fixed?: ScalingFixed;
  concurrency?: ScalingConcurrency;
}

export interface ModelDeployment {
  apiVersion: "modelplane.ai/v1alpha1";
  kind: "ModelDeployment";
  metadata: ObjectMeta;
  spec: {
    modelRef: { kind: string; name: string };
    environments: number;
    environmentSelector?: { matchLabels?: Record<string, string> };
    scaling?: Scaling;
  };
  status?: {
    conditions?: Condition[];
    endpoint?: { url: string };
    placements?: { total: number; ready: number };
    model?: { name: string };
  };
}

export interface KubeEvent {
  apiVersion: "v1";
  kind: "Event";
  metadata: ObjectMeta;
  type: "Normal" | "Warning";
  reason: string;
  message: string;
  firstTimestamp?: string;
  lastTimestamp?: string;
  count?: number;
  involvedObject: {
    apiVersion: string;
    kind: string;
    name: string;
    namespace?: string;
    uid?: string;
  };
}

export interface ModelPlacement {
  apiVersion: "modelplane.ai/v1alpha1";
  kind: "ModelPlacement";
  metadata: ObjectMeta;
  spec: {
    modelRef: { kind: string; name: string };
    inferenceEnvironmentRef: { name: string };
    scaling?: Scaling;
  };
  status?: {
    conditions?: Condition[];
    endpoint?: { url: string };
    resources?: { gpu?: { count: number } };
    model?: { name: string };
    servingProfile?: {
      name: string;
      engine?: { name: string; image: string };
    };
  };
}

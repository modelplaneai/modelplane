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
  count: number;
}

export interface InferenceEnvironment {
  apiVersion: "modelplane.ai/v1alpha1";
  kind: "InferenceEnvironment";
  metadata: ObjectMeta;
  spec: {
    backend: string;
    kserve?: {
      version?: string;
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
            };
          }>;
        };
      };
    };
  };
  status?: {
    conditions?: Condition[];
    providerConfigRef?: { name: string };
    gateway?: { address: string };
    capacity?: { backend: string; gpuPools?: GPUPool[] };
    namespace?: string;
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
    engine: string;
    vllm?: { image?: string; extraArgs?: string[] };
    resources: { vram: string; cpu?: string; memory?: string };
  };
  status?: {
    conditions?: Condition[];
  };
}

export interface ModelDeployment {
  apiVersion: "modelplane.ai/v1alpha1";
  kind: "ModelDeployment";
  metadata: ObjectMeta;
  spec: {
    modelRef: { kind: string; name: string };
    environments: number;
    environmentSelector?: { matchLabels?: Record<string, string> };
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
  };
  status?: {
    conditions?: Condition[];
    endpoint?: { url: string };
    resources?: { gpu?: { count: number } };
    model?: { name: string };
  };
}

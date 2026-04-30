import { type ReactNode } from "react";
import { render } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ApiContext, ChatContext, type ApiClient, type ChatFn } from "../api/context";
import type {
  ClusterModel,
  InferenceEnvironment,
  KubeList,
  ModelDeployment,
} from "../api/types";

// nopClient returns an ApiClient where every method resolves to an empty list
// or a sensible zero value. Override individual methods per test.
export function nopClient(overrides?: Partial<ApiClient>): ApiClient {
  return {
    listClusterModels: async () => emptyList(),
    listInferenceGateways: async () => emptyList(),
    listInferenceEnvironments: async () => emptyList(),
    listModelDeployments: async () => emptyList(),
    listModelPlacements: async () => emptyList(),
    listAllModelPlacements: async () => emptyList(),
    getModelDeployment: async () => ({ apiVersion: "modelplane.ai/v1alpha1", kind: "ModelDeployment", metadata: { name: "" }, spec: { modelRef: { kind: "", name: "" }, environments: 0 } }),
    createModelDeployment: async (_, md) => md as ModelDeployment,
    deleteModelDeployment: async () => {},
    listNamespaces: async () => emptyList(),
    listEvents: async () => emptyList(),
    ...overrides,
  };
}

// nopChat returns a ChatFn that yields nothing.
export async function* nopChat(): AsyncGenerator<string> {
  // No tokens.
}

function emptyList<T>(): KubeList<T> {
  return { items: [], metadata: {} };
}

// renderWithProviders wraps a component in the providers it needs: router,
// query client, API context, and chat context. Use this for page and component
// tests.
export function renderWithProviders(
  ui: ReactNode,
  options?: {
    client?: Partial<ApiClient>;
    chat?: ChatFn;
    route?: string;
  },
) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0 },
    },
  });

  const client = nopClient(options?.client);
  const chat = options?.chat ?? nopChat;

  return render(
    <ApiContext.Provider value={client}>
      <ChatContext.Provider value={chat}>
        <QueryClientProvider client={queryClient}>
          <MemoryRouter initialEntries={[options?.route ?? "/"]}>
            {ui}
          </MemoryRouter>
        </QueryClientProvider>
      </ChatContext.Provider>
    </ApiContext.Provider>,
  );
}

// Factory helpers for building test fixtures.

export function clusterModel(overrides?: {
  name?: string;
  modelName?: string;
  vram?: string;
  repo?: string;
  serving?: ClusterModel["spec"]["serving"];
}): ClusterModel {
  return {
    apiVersion: "modelplane.ai/v1alpha1",
    kind: "ClusterModel",
    metadata: { name: overrides?.name ?? "test-model" },
    spec: {
      model: { name: overrides?.modelName ?? "TestOrg/TestModel" },
      source: "HuggingFace",
      huggingFace: { repo: overrides?.repo ?? "TestOrg/TestModel" },
      resources: { vram: overrides?.vram ?? "2Gi" },
      serving: overrides?.serving ?? [
        { name: "vllm-kserve", engine: { name: "vLLM", image: "vllm/vllm-openai:v0.7.3" } },
      ],
    },
    status: { conditions: [{ type: "Ready", status: "True" }] },
  };
}

export function modelDeployment(overrides?: {
  name?: string;
  ns?: string;
  modelRef?: string;
  ready?: boolean;
  endpoint?: string;
}): ModelDeployment {
  return {
    apiVersion: "modelplane.ai/v1alpha1",
    kind: "ModelDeployment",
    metadata: {
      name: overrides?.name ?? "test-deploy",
      namespace: overrides?.ns ?? "ml-team",
      creationTimestamp: "2026-03-24T10:00:00Z",
    },
    spec: {
      modelRef: { kind: "ClusterModel", name: overrides?.modelRef ?? "test-model" },
      environments: 1,
    },
    status: {
      conditions: [{
        type: "Ready",
        status: overrides?.ready !== false ? "True" : "False",
        reason: overrides?.ready !== false ? "Available" : "Creating",
      }],
      endpoint: overrides?.endpoint ? { url: overrides.endpoint } : undefined,
      placements: { total: 1, ready: overrides?.ready !== false ? 1 : 0 },
      model: { name: "TestOrg/TestModel" },
    },
  };
}

export function inferenceEnvironment(overrides?: {
  name?: string;
  region?: string;
  gateway?: string;
  ready?: boolean;
}): InferenceEnvironment {
  return {
    apiVersion: "modelplane.ai/v1alpha1",
    kind: "InferenceEnvironment",
    metadata: {
      name: overrides?.name ?? "test-env",
      labels: {
        "modelplane.ai/environment": "true",
        ...(overrides?.region ? { "modelplane.ai/region": overrides.region } : { "modelplane.ai/region": "us-central1" }),
      },
    },
    spec: {
      cluster: { source: "GKE", gke: { project: "test-project", region: overrides?.region ?? "us-central1" } },
    },
    status: {
      conditions: [{
        type: "Ready",
        status: overrides?.ready !== false ? "True" : "False",
        reason: overrides?.ready !== false ? "Available" : "Creating",
      }],
      gateway: overrides?.gateway ? { address: overrides.gateway } : undefined,
      capacity: {
        gpuPools: [{ acceleratorType: "nvidia-l4", memory: "24Gi", countPerNode: 1, nodes: 1 }],
      },
      namespace: "ie-test-env",
    },
  };
}

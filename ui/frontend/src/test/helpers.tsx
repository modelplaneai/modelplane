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
  ModelPlacement,
  ObjectMeta,
} from "../api/types";

// nopClient returns an ApiClient where every method resolves to an empty list
// or a sensible zero value. Override individual methods per test.
export function nopClient(overrides?: Partial<ApiClient>): ApiClient {
  return {
    listClusterModels: async () => emptyList(),
    listInferenceEnvironments: async () => emptyList(),
    listModelDeployments: async () => emptyList(),
    listModelPlacements: async () => emptyList(),
    getModelDeployment: async () => ({ apiVersion: "modelplane.ai/v1alpha1", kind: "ModelDeployment", metadata: { name: "" }, spec: { modelRef: { kind: "", name: "" }, environments: 0 } }),
    createModelDeployment: async (_, md) => md as ModelDeployment,
    deleteModelDeployment: async () => {},
    createClusterModel: async (cm) => cm as ClusterModel,
    deleteClusterModel: async () => {},
    listNamespaces: async () => emptyList(),
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
  engine?: string;
  vram?: string;
  repo?: string;
}): ClusterModel {
  return {
    apiVersion: "modelplane.ai/v1alpha1",
    kind: "ClusterModel",
    metadata: { name: overrides?.name ?? "test-model" },
    spec: {
      model: { name: overrides?.modelName ?? "TestOrg/TestModel" },
      source: "HuggingFace",
      huggingFace: { repo: overrides?.repo ?? "TestOrg/TestModel" },
      engine: overrides?.engine ?? "vLLM",
      resources: { vram: overrides?.vram ?? "2Gi" },
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
    metadata: { name: overrides?.name ?? "test-env" },
    spec: {
      backend: "KServe",
      kserve: {
        version: "v0.16.0",
        cluster: {
          source: "GKE",
          gke: {
            project: "test-project",
            region: overrides?.region ?? "us-central1",
          },
        },
      },
    },
    status: {
      conditions: [{
        type: "Ready",
        status: overrides?.ready !== false ? "True" : "False",
        reason: overrides?.ready !== false ? "Available" : "Creating",
      }],
      gateway: overrides?.gateway ? { address: overrides.gateway } : undefined,
      capacity: {
        backend: "KServe",
        gpuPools: [{ acceleratorType: "nvidia-l4", memory: "24Gi", count: 1 }],
      },
      namespace: "ie-test-env",
    },
  };
}

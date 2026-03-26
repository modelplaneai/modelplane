import { createContext, useContext } from "react";
import type {
  ClusterModel,
  InferenceEnvironment,
  KubeEvent,
  KubeList,
  ModelDeployment,
  ModelPlacement,
  ObjectMeta,
} from "./types";
import * as defaultClient from "./client";

// ApiClient defines the operations the UI needs from the backend. Components
// access it via useApi() rather than importing client.ts directly. This makes
// every component testable — tests provide a fake client via the context.
export interface ApiClient {
  listClusterModels(): Promise<KubeList<ClusterModel>>;
  listInferenceEnvironments(): Promise<KubeList<InferenceEnvironment>>;
  listModelDeployments(ns: string): Promise<KubeList<ModelDeployment>>;
  listModelPlacements(ns: string): Promise<KubeList<ModelPlacement>>;
  listAllModelPlacements(): Promise<KubeList<ModelPlacement>>;
  getModelDeployment(ns: string, name: string): Promise<ModelDeployment>;
  createModelDeployment(ns: string, md: Partial<ModelDeployment>): Promise<ModelDeployment>;
  deleteModelDeployment(ns: string, name: string): Promise<void>;
  createClusterModel(cm: Partial<ClusterModel>): Promise<ClusterModel>;
  deleteClusterModel(name: string): Promise<void>;
  listNamespaces(): Promise<KubeList<{ metadata: ObjectMeta }>>;
  listEvents(ns: string, kind: string, name: string, uid?: string): Promise<KubeList<KubeEvent>>;
}

// ChatFn streams chat tokens from a model endpoint. Separated from ApiClient
// because it has a different shape (async generator vs promise).
export type ChatFn = (
  ns: string,
  name: string,
  model: string,
  messages: Array<{ role: string; content: string }>,
) => AsyncGenerator<string>;

export const ApiContext = createContext<ApiClient>(defaultClient);
export const ChatContext = createContext<ChatFn | null>(null);

export function useApi(): ApiClient {
  return useContext(ApiContext);
}

export function useChat(): ChatFn {
  const fn = useContext(ChatContext);
  if (!fn) {
    throw new Error("ChatContext not provided");
  }
  return fn;
}

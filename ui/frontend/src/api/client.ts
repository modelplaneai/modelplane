import type {
  ClusterModel,
  InferenceEnvironment,
  InferenceGateway,
  KubeEvent,
  KubeList,
  ModelDeployment,
  ModelPlacement,
  ObjectMeta,
} from "./types";

const BASE = "/api/k8s";
const MP = "apis/modelplane.ai/v1alpha1";

async function get<T>(path: string): Promise<T> {
  const resp = await fetch(`${BASE}/${path}`);
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`GET ${path}: ${resp.status} ${text}`);
  }
  return resp.json() as Promise<T>;
}

async function post<T>(path: string, body: unknown): Promise<T> {
  const resp = await fetch(`${BASE}/${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`POST ${path}: ${resp.status} ${text}`);
  }
  return resp.json() as Promise<T>;
}

async function del(path: string): Promise<void> {
  const resp = await fetch(`${BASE}/${path}`, { method: "DELETE" });
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`DELETE ${path}: ${resp.status} ${text}`);
  }
}

export function listClusterModels(): Promise<KubeList<ClusterModel>> {
  return get(`${MP}/clustermodels`);
}

export function listInferenceGateways(): Promise<
  KubeList<InferenceGateway>
> {
  return get(`${MP}/inferencegateways`);
}

export function listInferenceEnvironments(): Promise<
  KubeList<InferenceEnvironment>
> {
  return get(`${MP}/inferenceenvironments`);
}

export function listModelDeployments(
  ns: string,
): Promise<KubeList<ModelDeployment>> {
  return get(`${MP}/namespaces/${ns}/modeldeployments`);
}

export function listModelPlacements(
  ns: string,
): Promise<KubeList<ModelPlacement>> {
  return get(`${MP}/namespaces/${ns}/modelplacements`);
}

export function listAllModelPlacements(): Promise<KubeList<ModelPlacement>> {
  return get(`${MP}/modelplacements`);
}

export function getModelDeployment(
  ns: string,
  name: string,
): Promise<ModelDeployment> {
  return get(`${MP}/namespaces/${ns}/modeldeployments/${name}`);
}

export function createModelDeployment(
  ns: string,
  md: Partial<ModelDeployment>,
): Promise<ModelDeployment> {
  return post(`${MP}/namespaces/${ns}/modeldeployments`, md);
}

export function deleteModelDeployment(
  ns: string,
  name: string,
): Promise<void> {
  return del(`${MP}/namespaces/${ns}/modeldeployments/${name}`);
}

export function createClusterModel(
  cm: Partial<ClusterModel>,
): Promise<ClusterModel> {
  return post(`${MP}/clustermodels`, cm);
}

export function deleteClusterModel(name: string): Promise<void> {
  return del(`${MP}/clustermodels/${name}`);
}

export function listNamespaces(): Promise<
  KubeList<{ metadata: ObjectMeta }>
> {
  return get("api/v1/namespaces");
}

export function listEvents(
  ns: string,
  kind: string,
  name: string,
  uid?: string,
): Promise<KubeList<KubeEvent>> {
  let sel = `involvedObject.kind=${kind},involvedObject.name=${name}`;
  if (uid) sel += `,involvedObject.uid=${uid}`;
  return get(`api/v1/namespaces/${ns}/events?fieldSelector=${encodeURIComponent(sel)}`);
}

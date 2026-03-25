import { useQuery } from "@tanstack/react-query";
import { useApi } from "../api/context";
import type { KubeList, ModelDeployment } from "../api/types";

export function useDeployments(ns: string) {
  const api = useApi();
  return useQuery<KubeList<ModelDeployment>>({
    queryKey: ["modeldeployments", ns],
    queryFn: () => api.listModelDeployments(ns),
    enabled: !!ns,
  });
}

export function useDeployment(ns: string, name: string) {
  const api = useApi();
  return useQuery<ModelDeployment>({
    queryKey: ["modeldeployment", ns, name],
    queryFn: () => api.getModelDeployment(ns, name),
    enabled: !!ns && !!name,
  });
}

import { useQuery } from "@tanstack/react-query";
import { useApi } from "../api/context";
import type { ClusterModel, KubeList } from "../api/types";

export function useModels() {
  const api = useApi();
  return useQuery<KubeList<ClusterModel>>({
    queryKey: ["clustermodels"],
    queryFn: () => api.listClusterModels(),
  });
}

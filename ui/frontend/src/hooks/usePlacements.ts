import { useQuery } from "@tanstack/react-query";
import { useApi } from "../api/context";
import type { KubeList, ModelPlacement } from "../api/types";

export function usePlacements(ns: string) {
  const api = useApi();
  return useQuery<KubeList<ModelPlacement>>({
    queryKey: ["modelplacements", ns],
    queryFn: () => api.listModelPlacements(ns),
    enabled: !!ns,
  });
}

import { useQuery } from "@tanstack/react-query";
import { useApi } from "../api/context";
import type { KubeEvent, KubeList } from "../api/types";

export function useEvents(ns: string, kind: string, name: string, uid?: string) {
  const api = useApi();
  return useQuery<KubeList<KubeEvent>>({
    queryKey: ["events", ns, kind, name, uid],
    queryFn: () => api.listEvents(ns, kind, name, uid),
    enabled: !!ns && !!kind && !!name,
  });
}

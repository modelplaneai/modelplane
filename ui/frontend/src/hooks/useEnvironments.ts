import { useQuery } from "@tanstack/react-query";
import { useApi } from "../api/context";
import type { InferenceEnvironment, KubeList } from "../api/types";

export function useEnvironments() {
  const api = useApi();
  return useQuery<KubeList<InferenceEnvironment>>({
    queryKey: ["inferenceenvironments"],
    queryFn: () => api.listInferenceEnvironments(),
  });
}

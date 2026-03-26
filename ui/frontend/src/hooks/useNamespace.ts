import { createContext, useContext } from "react";
import { DEFAULT_NAMESPACE } from "../lib/config";

export interface NamespaceState {
  namespace: string;
  setNamespace: (ns: string) => void;
}

export const NamespaceContext = createContext<NamespaceState>({
  namespace: DEFAULT_NAMESPACE,
  setNamespace: () => {},
});

export function useNamespace(): NamespaceState {
  return useContext(NamespaceContext);
}

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ApiContext, ChatContext } from "./api/context";
import * as client from "./api/client";
import { streamChat } from "./api/chat";
import App from "./App";
import "./index.css";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      refetchInterval: 5000, // Poll every 5s for demo liveness.
      retry: 1,
    },
  },
});

// main.tsx is the composition root. It wires the real API client and chat
// function into context. Tests provide fakes via the same contexts.
createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ApiContext.Provider value={client}>
      <ChatContext.Provider value={streamChat}>
        <QueryClientProvider client={queryClient}>
          <BrowserRouter>
            <App />
          </BrowserRouter>
        </QueryClientProvider>
      </ChatContext.Provider>
    </ApiContext.Provider>
  </StrictMode>,
);

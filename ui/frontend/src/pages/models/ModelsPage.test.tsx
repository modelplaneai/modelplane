import { describe, it, expect } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import { renderWithProviders, clusterModel } from "../../test/helpers";
import { ModelsPage } from "./ModelsPage";

describe("ModelsPage", () => {
  it("renders model cards from the API", async () => {
    const models = [
      clusterModel({ name: "qwen-0.5b", modelName: "Qwen/Qwen2.5-0.5B-Instruct", vram: "2Gi" }),
      clusterModel({ name: "llama-70b", modelName: "meta-llama/Llama-3.1-70B", vram: "140Gi" }),
    ];

    renderWithProviders(<ModelsPage />, {
      client: {
        listClusterModels: async () => ({ items: models, metadata: {} }),
      },
    });

    await waitFor(() => {
      expect(screen.getByText("Qwen 2.5 0.5B Instruct")).toBeInTheDocument();
    });
    expect(screen.getByText("Llama 3.1 70B")).toBeInTheDocument();
    expect(screen.getByText("2Gi")).toBeInTheDocument();
    expect(screen.getByText("140Gi")).toBeInTheDocument();
  });

  it("shows loading state", () => {
    renderWithProviders(<ModelsPage />, {
      client: {
        listClusterModels: () => new Promise(() => {}), // never resolves
      },
    });
    expect(screen.getByText(/Loading models/)).toBeInTheDocument();
  });

  it("shows error state when API fails", async () => {
    renderWithProviders(<ModelsPage />, {
      client: {
        listClusterModels: async () => { throw new Error("connection refused"); },
      },
    });
    await waitFor(() => {
      expect(screen.getByText(/connection refused/)).toBeInTheDocument();
    });
  });

  it("shows empty state when no models exist", async () => {
    renderWithProviders(<ModelsPage />); // nopClient returns empty list
    await waitFor(() => {
      expect(screen.getByText(/No models found/)).toBeInTheDocument();
    });
  });
});

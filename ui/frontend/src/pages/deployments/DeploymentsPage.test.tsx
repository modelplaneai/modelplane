import { describe, it, expect } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import { renderWithProviders, modelDeployment } from "../../test/helpers";
import { DeploymentsPage } from "./DeploymentsPage";

describe("DeploymentsPage", () => {
  it("renders deployment rows from the API", async () => {
    const deployments = [
      modelDeployment({
        name: "qwen-deploy",
        ns: "ml-team",
        modelRef: "qwen-0.5b",
        ready: true,
        endpoint: "https://qwen.example.com",
      }),
      modelDeployment({
        name: "llama-deploy",
        ns: "ml-team",
        modelRef: "llama-70b",
        ready: false,
      }),
    ];

    renderWithProviders(<DeploymentsPage />, {
      client: {
        listModelDeployments: async () => ({ items: deployments, metadata: {} }),
      },
    });

    await waitFor(() => {
      expect(screen.getByText("qwen-deploy")).toBeInTheDocument();
    });
    expect(screen.getByText("llama-deploy")).toBeInTheDocument();

    // Model name comes from status.model.name (set by the fixture helper).
    expect(screen.getAllByText("TestOrg/TestModel")).toHaveLength(2);

    // Endpoint renders for the first deployment.
    expect(screen.getByText("https://qwen.example.com")).toBeInTheDocument();

    // Status text derives from conditions.
    expect(screen.getByText("Ready")).toBeInTheDocument();
    expect(screen.getByText("Creating")).toBeInTheDocument();

    // Placement counts: ready deployment shows "1/1", not-ready shows "0/1".
    expect(screen.getByText("1/1")).toBeInTheDocument();
    expect(screen.getByText("0/1")).toBeInTheDocument();
  });

  it("renders status dots for each deployment", async () => {
    const deployments = [
      modelDeployment({ name: "healthy", ready: true }),
      modelDeployment({ name: "unhealthy", ready: false }),
    ];

    renderWithProviders(<DeploymentsPage />, {
      client: {
        listModelDeployments: async () => ({ items: deployments, metadata: {} }),
      },
    });

    await waitFor(() => {
      expect(screen.getByText("healthy")).toBeInTheDocument();
    });

    // StatusDot renders a <span> with a title matching the status level.
    const readyDots = screen.getAllByTitle("ready");
    expect(readyDots.length).toBeGreaterThanOrEqual(1);
    const creatingDots = screen.getAllByTitle("creating");
    expect(creatingDots.length).toBeGreaterThanOrEqual(1);
  });

  it("shows loading state", () => {
    renderWithProviders(<DeploymentsPage />, {
      client: {
        listModelDeployments: () => new Promise(() => {}), // never resolves
      },
    });
    expect(screen.getByText(/Loading deployments/)).toBeInTheDocument();
  });

  it("shows error state when API fails", async () => {
    renderWithProviders(<DeploymentsPage />, {
      client: {
        listModelDeployments: async () => { throw new Error("network timeout"); },
      },
    });
    await waitFor(() => {
      expect(screen.getByText(/network timeout/)).toBeInTheDocument();
    });
  });

  it("shows empty state when no deployments exist", async () => {
    renderWithProviders(<DeploymentsPage />); // nopClient returns empty list
    await waitFor(() => {
      expect(screen.getByText(/No deployments in this namespace/)).toBeInTheDocument();
    });
  });
});

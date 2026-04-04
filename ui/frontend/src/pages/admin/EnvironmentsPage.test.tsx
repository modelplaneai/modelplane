import { describe, it, expect } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderWithProviders, inferenceEnvironment } from "../../test/helpers";
import { EnvironmentsPage } from "./EnvironmentsPage";

describe("EnvironmentsPage", () => {
  it("renders environment rows from the API", async () => {
    const environments = [
      inferenceEnvironment({
        name: "us-central",
        region: "us-central1",
        gateway: "10.0.0.1",
        ready: true,
      }),
      inferenceEnvironment({
        name: "eu-west",
        region: "europe-west1",
        ready: false,
      }),
    ];

    renderWithProviders(<EnvironmentsPage />, {
      client: {
        listInferenceEnvironments: async () => ({ items: environments, metadata: {} }),
      },
    });

    await waitFor(() => {
      expect(screen.getByText("us-central")).toBeInTheDocument();
    });
    expect(screen.getByText("eu-west")).toBeInTheDocument();

    // Region renders from the modelplane.ai/region label.
    expect(screen.getByText("us-central1")).toBeInTheDocument();
    expect(screen.getByText("europe-west1")).toBeInTheDocument();

    // Backend renders from spec.backend.
    expect(screen.getAllByText("KServe")).toHaveLength(2);

    // Gateway address renders for the first environment.
    expect(screen.getByText("10.0.0.1")).toBeInTheDocument();

    // Status text derives from conditions.
    expect(screen.getByText("Ready")).toBeInTheDocument();
    expect(screen.getByText("Creating")).toBeInTheDocument();
  });

  it("expands a row to show conditions and GPU pools on click", async () => {
    const user = userEvent.setup();

    const environments = [
      inferenceEnvironment({
        name: "us-central",
        region: "us-central1",
        gateway: "10.0.0.1",
        ready: true,
      }),
    ];

    renderWithProviders(<EnvironmentsPage />, {
      client: {
        listInferenceEnvironments: async () => ({ items: environments, metadata: {} }),
      },
    });

    await waitFor(() => {
      expect(screen.getByText("us-central")).toBeInTheDocument();
    });

    // Detail row should not be visible before clicking.
    expect(screen.queryByText("Conditions")).not.toBeInTheDocument();
    expect(screen.queryByText("GPU Pools")).not.toBeInTheDocument();

    // Click the row to expand it.
    await user.click(screen.getAllByText("us-central")[0]);

    // Conditions section appears with the Ready condition.
    expect(screen.getByText("Conditions")).toBeInTheDocument();
    expect(screen.getByText(/Available/)).toBeInTheDocument();

    // GPU Pools section appears with the fixture's nvidia-l4 pool.
    expect(screen.getByText("GPU Pools")).toBeInTheDocument();
    expect(screen.getByText("nvidia-l4")).toBeInTheDocument();
    expect(screen.getByText(/24Gi VRAM\/GPU/)).toBeInTheDocument();

    // Backend version from spec.kserve.version.
    expect(screen.getByText("v0.16.0")).toBeInTheDocument();

    // Internal namespace from status.namespace.
    expect(screen.getByText("ie-test-env")).toBeInTheDocument();
  });

  it("collapses an expanded row on second click", async () => {
    const user = userEvent.setup();

    const environments = [
      inferenceEnvironment({ name: "collapse-env", ready: true }),
    ];

    const { container } = renderWithProviders(<EnvironmentsPage />, {
      client: {
        listInferenceEnvironments: async () => ({ items: environments, metadata: {} }),
      },
    });

    const view = within(container);

    await waitFor(() => {
      expect(view.getAllByText("collapse-env").length).toBeGreaterThanOrEqual(1);
    });

    // Expand — click the first matching row text.
    await user.click(view.getAllByText("collapse-env")[0]);

    await waitFor(() => {
      expect(view.queryByText("Conditions")).toBeInTheDocument();
    });

    // Collapse — click the same row again.
    await user.click(view.getAllByText("collapse-env")[0]);

    await waitFor(() => {
      expect(view.queryByText("Conditions")).not.toBeInTheDocument();
    });
  });

  it("shows loading state", () => {
    renderWithProviders(<EnvironmentsPage />, {
      client: {
        listInferenceEnvironments: () => new Promise(() => {}), // never resolves
      },
    });
    expect(screen.getByText(/Loading environments/)).toBeInTheDocument();
  });

  it("shows error state when API fails", async () => {
    renderWithProviders(<EnvironmentsPage />, {
      client: {
        listInferenceEnvironments: async () => { throw new Error("forbidden"); },
      },
    });
    await waitFor(() => {
      expect(screen.getByText(/forbidden/)).toBeInTheDocument();
    });
  });

  it("shows empty state when no environments exist", async () => {
    renderWithProviders(<EnvironmentsPage />); // nopClient returns empty list
    await waitFor(() => {
      expect(screen.getByText(/No inference environments found/)).toBeInTheDocument();
    });
  });
});

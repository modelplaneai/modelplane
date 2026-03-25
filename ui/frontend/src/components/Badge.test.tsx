import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { Badge } from "./Badge";

describe("Badge", () => {
  it("renders children", () => {
    render(<Badge>Active</Badge>);
    expect(screen.getByText("Active")).toBeInTheDocument();
  });

  it("applies neutral classes by default", () => {
    render(<Badge>Default</Badge>);
    const badge = screen.getByText("Default");
    expect(badge).toHaveClass("bg-muted/15", "text-muted-hi");
  });

  it("applies purple variant classes", () => {
    render(<Badge variant="purple">GPU</Badge>);
    const badge = screen.getByText("GPU");
    expect(badge).toHaveClass("bg-purple/15", "text-purple-hi");
  });

  it("applies cyan variant classes", () => {
    render(<Badge variant="cyan">Serving</Badge>);
    const badge = screen.getByText("Serving");
    expect(badge).toHaveClass("bg-cyan/15", "text-cyan");
  });

  it("applies green variant classes", () => {
    render(<Badge variant="green">Ready</Badge>);
    const badge = screen.getByText("Ready");
    expect(badge).toHaveClass("bg-green/15", "text-green");
  });

  it("always has base styling", () => {
    render(<Badge>Base</Badge>);
    const badge = screen.getByText("Base");
    expect(badge).toHaveClass("rounded-full", "text-xs", "font-mono", "uppercase");
  });
});

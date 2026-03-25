import { describe, it, expect } from "vitest";
import { render, screen, cleanup } from "@testing-library/react";
import { StatusDot } from "./StatusDot";

describe("StatusDot", () => {
  it("renders with green class for ready status", () => {
    render(<StatusDot status="ready" />);
    const dot = screen.getByTitle("ready");
    expect(dot).toHaveClass("bg-green");
    cleanup();
  });

  it("renders with purple class and pulse for creating status", () => {
    render(<StatusDot status="creating" />);
    const dot = screen.getByTitle("creating");
    expect(dot).toHaveClass("bg-purple");
    expect(dot).toHaveClass("animate-pulse");
    cleanup();
  });

  it("renders with red class for error status", () => {
    render(<StatusDot status="error" />);
    const dot = screen.getByTitle("error");
    expect(dot).toHaveClass("bg-red");
    cleanup();
  });

  it("renders with muted class for unknown status", () => {
    render(<StatusDot status="unknown" />);
    const dot = screen.getByTitle("unknown");
    expect(dot).toHaveClass("bg-muted");
    cleanup();
  });

  it("renders as an inline-block rounded element", () => {
    render(<StatusDot status="ready" />);
    const dot = screen.getByTitle("ready");
    expect(dot).toHaveClass("inline-block", "rounded-full");
    cleanup();
  });
});

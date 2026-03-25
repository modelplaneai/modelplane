import { describe, it, expect, vi } from "vitest";
import { render, screen, cleanup } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Button } from "./Button";

describe("Button", () => {
  it("renders children", () => {
    render(<Button>Click me</Button>);
    expect(screen.getByRole("button", { name: "Click me" })).toBeInTheDocument();
    cleanup();
  });

  it("defaults to primary variant", () => {
    render(<Button>Primary</Button>);
    const btn = screen.getByRole("button", { name: "Primary" });
    expect(btn).toHaveClass("bg-gradient-to-r", "from-purple", "to-purple-hi");
    cleanup();
  });

  it("applies ghost variant classes", () => {
    render(<Button variant="ghost">Ghost</Button>);
    const btn = screen.getByRole("button", { name: "Ghost" });
    expect(btn).toHaveClass("border", "border-border", "text-muted-hi");
    cleanup();
  });

  it("applies danger variant classes", () => {
    render(<Button variant="danger">Delete</Button>);
    const btn = screen.getByRole("button", { name: "Delete" });
    expect(btn).toHaveClass("text-red");
    cleanup();
  });

  it("calls onClick when clicked", async () => {
    const user = userEvent.setup();
    const handleClick = vi.fn();
    render(<Button onClick={handleClick}>Press</Button>);
    await user.click(screen.getByRole("button", { name: "Press" }));
    expect(handleClick).toHaveBeenCalledOnce();
    cleanup();
  });

  it("does not call onClick when disabled", async () => {
    const user = userEvent.setup();
    const handleClick = vi.fn();
    render(<Button onClick={handleClick} disabled>Disabled</Button>);
    await user.click(screen.getByRole("button", { name: "Disabled" }));
    expect(handleClick).not.toHaveBeenCalled();
    cleanup();
  });

  it("applies disabled styling", () => {
    render(<Button disabled>Nope</Button>);
    const btn = screen.getByRole("button", { name: "Nope" });
    expect(btn).toBeDisabled();
    expect(btn).toHaveClass("disabled:opacity-50", "disabled:cursor-not-allowed");
    cleanup();
  });

  it("defaults to type=button", () => {
    render(<Button>Btn</Button>);
    expect(screen.getByRole("button", { name: "Btn" })).toHaveAttribute("type", "button");
    cleanup();
  });

  it("respects type=submit", () => {
    render(<Button type="submit">Submit</Button>);
    expect(screen.getByRole("button", { name: "Submit" })).toHaveAttribute("type", "submit");
    cleanup();
  });

  it("applies additional className", () => {
    render(<Button className="mt-4">Extra</Button>);
    expect(screen.getByRole("button", { name: "Extra" })).toHaveClass("mt-4");
    cleanup();
  });
});

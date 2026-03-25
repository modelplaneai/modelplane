import type { ReactNode } from "react";

interface BadgeProps {
  children: ReactNode;
  variant?: "purple" | "cyan" | "green" | "neutral";
}

const variants: Record<NonNullable<BadgeProps["variant"]>, string> = {
  purple: "bg-purple/15 text-purple-hi",
  cyan: "bg-cyan/15 text-cyan",
  green: "bg-green/15 text-green",
  neutral: "bg-muted/15 text-muted-hi",
};

export function Badge({ children, variant = "neutral" }: BadgeProps) {
  return (
    <span
      className={`rounded-full px-2 py-0.5 text-xs font-mono uppercase tracking-wider ${variants[variant]}`}
    >
      {children}
    </span>
  );
}

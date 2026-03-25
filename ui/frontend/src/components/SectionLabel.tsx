import type { ReactNode } from "react";

interface SectionLabelProps {
  children: ReactNode;
}

export function SectionLabel({ children }: SectionLabelProps) {
  return (
    <h3 className="font-mono text-[11px] uppercase tracking-[0.16em] text-purple mb-4">
      {children}
    </h3>
  );
}

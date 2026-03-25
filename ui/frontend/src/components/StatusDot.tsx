interface StatusDotProps {
  status: "ready" | "creating" | "error" | "unknown";
}

const styles: Record<StatusDotProps["status"], string> = {
  ready: "bg-green shadow-[0_0_6px_rgba(52,211,153,0.6)]",
  creating: "bg-purple animate-pulse",
  error: "bg-red",
  unknown: "bg-muted",
};

export function StatusDot({ status }: StatusDotProps) {
  return (
    <span
      className={`inline-block w-2 h-2 rounded-full ${styles[status]}`}
      title={status}
    />
  );
}

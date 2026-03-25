import { useState } from "react";

interface CurlSnippetProps {
  url: string;
  model: string;
}

export function CurlSnippet({ url, model }: CurlSnippetProps) {
  const [copied, setCopied] = useState(false);

  const command = `curl ${url} -H "Content-Type: application/json" -d '{"model":"${model}","messages":[{"role":"user","content":"Hello"}]}'`;

  const copy = async () => {
    await navigator.clipboard.writeText(command);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div className="relative bg-bg-mid border border-border rounded-lg p-4 font-mono text-xs text-muted-hi overflow-x-auto">
      <pre className="whitespace-pre-wrap break-all">{command}</pre>
      <button
        onClick={copy}
        className="absolute top-2 right-2 text-muted hover:text-text transition text-xs px-2 py-1 rounded border border-border hover:border-border-hi"
      >
        {copied ? "Copied" : "Copy"}
      </button>
    </div>
  );
}

interface ChatMessage {
  role: string;
  content: string;
}

interface ChatDelta {
  choices?: Array<{
    delta?: { content?: string };
  }>;
}

export async function* streamChat(
  namespace: string,
  deploymentName: string,
  model: string,
  messages: ChatMessage[],
): AsyncGenerator<string> {
  const resp = await fetch(`/api/chat/${namespace}/${deploymentName}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ model, messages, stream: true }),
  });

  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`chat: ${resp.status} ${text}`);
  }

  const body = resp.body;
  if (!body) {
    throw new Error("chat: response body is null");
  }

  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      // Keep the last potentially incomplete line in the buffer.
      buffer = lines.pop() ?? "";

      for (const line of lines) {
        const trimmed = line.trim();
        if (!trimmed || trimmed.startsWith(":")) {
          // Empty line or SSE comment, skip.
          continue;
        }

        if (!trimmed.startsWith("data: ")) {
          continue;
        }

        const payload = trimmed.slice("data: ".length);
        if (payload === "[DONE]") {
          return;
        }

        const chunk = JSON.parse(payload) as ChatDelta;
        const content = chunk.choices?.[0]?.delta?.content;
        if (content) {
          yield content;
        }
      }
    }
  } finally {
    reader.releaseLock();
  }
}

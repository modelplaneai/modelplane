import { useState, useRef, useEffect, useCallback, useReducer } from "react";
import { useChat } from "../api/context";
import { Card } from "./Card";
import { Button } from "./Button";

interface ChatMsg {
  role: "user" | "assistant";
  content: string;
}

interface Props {
  namespace: string;
  deployment: string;
  model: string;
}

type Action =
  | { type: "send"; content: string }
  | { type: "token"; content: string }
  | { type: "error"; message: string }
  | { type: "done" };

interface ChatState {
  messages: ChatMsg[];
  streaming: boolean;
}

function chatReducer(state: ChatState, action: Action): ChatState {
  switch (action.type) {
    case "send":
      return {
        messages: [
          ...state.messages,
          { role: "user", content: action.content },
          { role: "assistant", content: "" },
        ],
        streaming: true,
      };
    case "token": {
      const msgs = [...state.messages];
      const last = msgs[msgs.length - 1];
      msgs[msgs.length - 1] = { ...last, content: last.content + action.content };
      return { ...state, messages: msgs };
    }
    case "error": {
      const msgs = [...state.messages];
      const last = msgs[msgs.length - 1];
      msgs[msgs.length - 1] = {
        ...last,
        content: last.content + `\n\n[Error: ${action.message}]`,
      };
      return { messages: msgs, streaming: false };
    }
    case "done":
      return { ...state, streaming: false };
  }
}

export function ChatWidget({ namespace, deployment, model }: Props) {
  const chat = useChat();
  const [{ messages, streaming }, dispatch] = useReducer(chatReducer, {
    messages: [],
    streaming: false,
  });
  const [input, setInput] = useState("");
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [messages]);

  const send = useCallback(async () => {
    const text = input.trim();
    if (!text || streaming) return;

    dispatch({ type: "send", content: text });
    setInput("");

    try {
      const history = [...messages, { role: "user" as const, content: text }];
      const apiMessages = history.map((m) => ({ role: m.role, content: m.content }));
      for await (const token of chat(namespace, deployment, model, apiMessages)) {
        dispatch({ type: "token", content: token });
      }
      dispatch({ type: "done" });
    } catch (err) {
      dispatch({ type: "error", message: err instanceof Error ? err.message : "Stream failed" });
    }
  }, [input, streaming, messages, namespace, deployment, model, chat]);

  return (
    <Card className="flex flex-col h-[420px]">
      <div ref={scrollRef} className="flex-1 overflow-y-auto space-y-3 mb-4">
        {messages.length === 0 && (
          <p className="text-muted text-sm text-center py-8">
            Send a message to start chatting with the model.
          </p>
        )}
        {messages.map((msg, i) => (
          <div key={i} className={`flex ${msg.role === "user" ? "justify-end" : "justify-start"}`}>
            <div
              className={`max-w-[80%] rounded-lg px-3 py-2 text-sm whitespace-pre-wrap ${
                msg.role === "user" ? "bg-purple/20 text-text" : "bg-bg-mid text-muted-hi"
              }`}
            >
              {msg.content || (streaming && msg.role === "assistant" ? (
                <span className="animate-pulse text-muted">…</span>
              ) : null)}
            </div>
          </div>
        ))}
      </div>

      <div className="flex gap-2">
        <input
          type="text"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && !e.shiftKey && send()}
          placeholder="Type a message…"
          disabled={streaming}
          className="flex-1 bg-bg-mid border border-border rounded-lg px-3 py-2 text-sm text-text placeholder:text-muted focus:outline-none focus:border-border-hi disabled:opacity-50"
        />
        <Button onClick={send} disabled={streaming || !input.trim()}>
          Send
        </Button>
      </div>
    </Card>
  );
}

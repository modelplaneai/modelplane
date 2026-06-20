// Modelplane docs MCP server.
//
// A zero-dependency Model Context Protocol server that lets AI assistants
// search and read the Modelplane documentation. It implements the Streamable
// HTTP transport (JSON-RPC 2.0 over POST) by hand so it ships as a single
// Vercel function with no npm install: the docs site builds in a sandboxed Nix
// derivation with `installCommand: true`, which skips dependency installation.
//
// The corpus is the llms.json the Hugo build publishes. We fetch it once per
// cold start, chunk every page by heading, and rank chunks with BM25. The
// search is lexical, not semantic: the corpus is small and the calling model
// does the semantic reasoning over the candidates we return. See
// content/ai-tools.md for the user-facing connection guide.

const CORPUS_URL =
  process.env.DOCS_LLMS_JSON_URL || "https://docs.modelplane.ai/llms.json";
const PROTOCOL_VERSION = "2025-06-18";
const SERVER_INFO = { name: "modelplane-docs", version: "0.1.0" };

// ── Corpus loading and indexing ──────────────────────────────────────────────

let indexPromise = null;

// Cache the built index for the lifetime of the warm function instance.
function loadIndex() {
  if (!indexPromise) {
    indexPromise = buildIndex().catch((err) => {
      // Don't cache a failed load: let the next request retry.
      indexPromise = null;
      throw err;
    });
  }
  return indexPromise;
}

async function buildIndex() {
  const res = await fetch(CORPUS_URL, { headers: { accept: "application/json" } });
  if (!res.ok) {
    throw new Error(`Failed to fetch corpus ${CORPUS_URL}: ${res.status}`);
  }
  const corpus = await res.json();
  const pages = Array.isArray(corpus.pages) ? corpus.pages : [];

  const chunks = [];
  for (const page of pages) {
    for (const chunk of chunkPage(page)) {
      chunks.push(chunk);
    }
  }

  // BM25 statistics.
  const df = new Map(); // term -> number of chunks containing it
  let totalLen = 0;
  for (const chunk of chunks) {
    chunk.tokens = tokenize(chunk.text);
    chunk.len = chunk.tokens.length;
    totalLen += chunk.len;
    chunk.tf = termFreqs(chunk.tokens);
    for (const term of chunk.tf.keys()) {
      df.set(term, (df.get(term) || 0) + 1);
    }
  }
  const avgLen = chunks.length ? totalLen / chunks.length : 0;

  return { pages, chunks, df, avgLen };
}

// Strip HTML comments (e.g. Vale `<!-- vale ... -->` directives) that are
// tooling noise, not documentation.
function stripComments(text) {
  return String(text).replace(/<!--[\s\S]*?-->/g, "");
}

// Split a page into chunks at Markdown headings so search ranks at section
// granularity. The page intro (text before the first heading) is its own chunk.
function chunkPage(page) {
  const content = stripComments(page.content || "");
  const lines = content.split("\n");
  const chunks = [];
  let heading = page.title || "";
  let buf = [];

  const flush = () => {
    const text = buf.join("\n").trim();
    if (text || chunks.length === 0) {
      const anchor = chunks.length === 0 ? "" : "#" + slugify(heading);
      chunks.push({
        pageTitle: page.title || "",
        heading,
        url: (page.url || "") + anchor,
        description: page.description || "",
        text: (heading ? heading + "\n" : "") + text,
      });
    }
    buf = [];
  };

  for (const line of lines) {
    const m = /^(#{1,6})\s+(.*)$/.exec(line);
    if (m) {
      flush();
      heading = m[2].trim();
    } else {
      buf.push(line);
    }
  }
  flush();
  return chunks.filter((c) => c.text.trim().length > 0);
}

const STOPWORDS = new Set(
  ("a an and are as at be by for from has have in into is it its of on or that the to " +
    "with you your this these those").split(" ")
);

function tokenize(text) {
  const out = [];
  for (const raw of String(text).toLowerCase().split(/[^a-z0-9]+/)) {
    if (!raw || raw.length < 2 || STOPWORDS.has(raw)) continue;
    out.push(stem(raw));
  }
  return out;
}

// Deliberately light stemming: fold common English suffixes so "deployments",
// "deploying", and "deployed" rank together without a full stemmer dependency.
function stem(t) {
  if (t.length > 4) {
    if (t.endsWith("ing")) return t.slice(0, -3);
    if (t.endsWith("ed")) return t.slice(0, -2);
    if (t.endsWith("ies")) return t.slice(0, -3) + "y";
    if (t.endsWith("es")) return t.slice(0, -2);
    if (t.endsWith("s")) return t.slice(0, -1);
  }
  return t;
}

function termFreqs(tokens) {
  const tf = new Map();
  for (const t of tokens) tf.set(t, (tf.get(t) || 0) + 1);
  return tf;
}

function slugify(s) {
  return String(s)
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
}

// ── Search ───────────────────────────────────────────────────────────────────

function search(index, query, limit) {
  const { chunks, df, avgLen } = index;
  const k1 = 1.5;
  const b = 0.75;
  const N = chunks.length;
  const qTerms = [...new Set(tokenize(query))];
  if (qTerms.length === 0) return [];

  const scored = [];
  for (const chunk of chunks) {
    let score = 0;
    for (const term of qTerms) {
      const tf = chunk.tf.get(term);
      if (!tf) continue;
      const n = df.get(term) || 0;
      const idf = Math.log(1 + (N - n + 0.5) / (n + 0.5));
      const denom = tf + k1 * (1 - b + (b * chunk.len) / (avgLen || 1));
      score += idf * ((tf * (k1 + 1)) / denom);
    }
    if (score > 0) scored.push({ chunk, score });
  }

  scored.sort((a, b) => b.score - a.score);
  return scored.slice(0, limit).map(({ chunk, score }) => ({
    title: chunk.pageTitle,
    heading: chunk.heading,
    url: chunk.url,
    score: Number(score.toFixed(3)),
    snippet: snippet(chunk.text),
  }));
}

function snippet(text, max = 360) {
  const clean = text.replace(/\s+/g, " ").trim();
  return clean.length > max ? clean.slice(0, max).trimEnd() + "..." : clean;
}

// ── Tools ──────────────────────────────────────────────────────────────────--

const TOOLS = [
  {
    name: "search_modelplane_docs",
    description:
      "Search the Modelplane documentation and return the most relevant sections " +
      "with their titles, canonical URLs, and snippets. Modelplane is the open " +
      "source control plane for AI model serving across a fleet of GPU clusters. " +
      "Use this to ground answers about Modelplane's CRDs (ModelDeployment, " +
      "InferenceCluster, InferenceClass, ModelService, and others), scheduling, " +
      "and setup in the current docs.",
    inputSchema: {
      type: "object",
      properties: {
        query: { type: "string", description: "Search query." },
        limit: {
          type: "integer",
          description: "Maximum number of results (default 5, max 20).",
          default: 5,
        },
      },
      required: ["query"],
    },
  },
  {
    name: "get_modelplane_doc",
    description:
      "Return the full Markdown of a single Modelplane documentation page by its " +
      "URL or path (for example /models/model-deployment/), as returned by " +
      "search_modelplane_docs.",
    inputSchema: {
      type: "object",
      properties: {
        path: {
          type: "string",
          description: "Page URL or path, e.g. /models/model-deployment/.",
        },
      },
      required: ["path"],
    },
  },
];

function normalizePath(p) {
  let s = String(p || "").trim();
  s = s.replace(/^https?:\/\/[^/]+/, ""); // strip origin if a full URL
  s = s.replace(/#.*$/, ""); // strip anchor
  s = s.replace(/index\.md$/, "").replace(/\.md$/, "");
  if (!s.startsWith("/")) s = "/" + s;
  if (!s.endsWith("/")) s += "/";
  return s;
}

async function callTool(name, args) {
  const index = await loadIndex();

  if (name === "search_modelplane_docs") {
    const query = String((args && args.query) || "").trim();
    if (!query) return toolError("query is required");
    let limit = Number((args && args.limit) || 5);
    if (!Number.isFinite(limit) || limit < 1) limit = 5;
    limit = Math.min(limit, 20);

    const results = search(index, query, limit);
    if (results.length === 0) {
      return toolText(`No results for "${query}".`);
    }
    const body = results
      .map(
        (r, i) =>
          `${i + 1}. ${r.title}${r.heading && r.heading !== r.title ? " — " + r.heading : ""}\n` +
          `   ${r.url}\n   ${r.snippet}`
      )
      .join("\n\n");
    return toolText(`Top ${results.length} results for "${query}":\n\n${body}`);
  }

  if (name === "get_modelplane_doc") {
    const want = normalizePath(args && args.path);
    const page = index.pages.find((p) => normalizePath(p.url || p.path) === want);
    if (!page) {
      return toolError(
        `No page found for "${(args && args.path) || ""}". Use search_modelplane_docs to find a path.`
      );
    }
    const header = `# ${page.title}\n${page.url}\n\n`;
    return toolText(header + stripComments(page.content || "").trim());
  }

  return toolError(`Unknown tool: ${name}`);
}

function toolText(text) {
  return { content: [{ type: "text", text }] };
}

function toolError(text) {
  return { content: [{ type: "text", text }], isError: true };
}

// ── JSON-RPC dispatch ─────────────────────────────────────────────────────────

const JSONRPC_VERSION = "2.0";

async function handleMessage(msg) {
  // Notifications have no id and expect no response.
  const isNotification = msg.id === undefined || msg.id === null;
  const respond = (result) => ({ jsonrpc: JSONRPC_VERSION, id: msg.id, result });
  const fail = (code, message) => ({
    jsonrpc: JSONRPC_VERSION,
    id: msg.id ?? null,
    error: { code, message },
  });

  try {
    switch (msg.method) {
      case "initialize":
        return respond({
          protocolVersion:
            (msg.params && msg.params.protocolVersion) || PROTOCOL_VERSION,
          capabilities: { tools: { listChanged: false } },
          serverInfo: SERVER_INFO,
        });
      case "notifications/initialized":
      case "notifications/cancelled":
        return null; // no response for notifications
      case "ping":
        return respond({});
      case "tools/list":
        return respond({ tools: TOOLS });
      case "tools/call": {
        const params = msg.params || {};
        const result = await callTool(params.name, params.arguments || {});
        return respond(result);
      }
      default:
        if (isNotification) return null;
        return fail(-32601, `Method not found: ${msg.method}`);
    }
  } catch (err) {
    if (isNotification) return null;
    return fail(-32603, `Internal error: ${err && err.message ? err.message : err}`);
  }
}

// ── HTTP transport ────────────────────────────────────────────────────────────

function setCors(res) {
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader("Access-Control-Allow-Methods", "POST, GET, OPTIONS");
  res.setHeader(
    "Access-Control-Allow-Headers",
    "Content-Type, Mcp-Session-Id, MCP-Protocol-Version, Authorization"
  );
}

async function readBody(req) {
  if (req.body !== undefined && req.body !== null && req.body !== "") {
    return typeof req.body === "string" ? JSON.parse(req.body) : req.body;
  }
  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
  const raw = Buffer.concat(chunks).toString("utf8");
  return raw ? JSON.parse(raw) : null;
}

module.exports = async function handler(req, res) {
  setCors(res);

  if (req.method === "OPTIONS") {
    res.statusCode = 204;
    return res.end();
  }

  // No server-initiated streams: this server is stateless and request/response.
  if (req.method === "GET") {
    res.statusCode = 405;
    res.setHeader("Allow", "POST, OPTIONS");
    return res.end("Method Not Allowed");
  }

  if (req.method !== "POST") {
    res.statusCode = 405;
    res.setHeader("Allow", "POST, OPTIONS");
    return res.end("Method Not Allowed");
  }

  let payload;
  try {
    payload = await readBody(req);
  } catch (err) {
    res.statusCode = 400;
    res.setHeader("Content-Type", "application/json");
    return res.end(
      JSON.stringify({
        jsonrpc: JSONRPC_VERSION,
        id: null,
        error: { code: -32700, message: "Parse error" },
      })
    );
  }

  const messages = Array.isArray(payload) ? payload : [payload];
  const responses = [];
  for (const msg of messages) {
    if (!msg || typeof msg !== "object") continue;
    const r = await handleMessage(msg);
    if (r !== null) responses.push(r);
  }

  // All inputs were notifications: acknowledge with 202 and no body.
  if (responses.length === 0) {
    res.statusCode = 202;
    return res.end();
  }

  res.statusCode = 200;
  res.setHeader("Content-Type", "application/json");
  const out = Array.isArray(payload) ? responses : responses[0];
  res.end(JSON.stringify(out));
};

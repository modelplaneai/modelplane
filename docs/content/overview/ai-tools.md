---
title: Working with AI tools
weight: 40
description: Connect AI assistants and coding agents to the Modelplane docs through MCP, Markdown, and llms.txt.
---

The Modelplane docs are built to be read by AI assistants as well as people. You can connect a coding agent directly to this site, pull any page as Markdown, or point a model at a single index file that lists the whole documentation set. Every page also carries a **Copy page** menu next to its title with the same shortcuts.

## Connect to the MCP server

The documentation MCP server lets an assistant search these docs and read any page in real time, so its answers track the current content instead of its training data. It exposes two tools:

- `search_modelplane_docs`: search the docs and get back the most relevant sections with their titles, URLs, and snippets.
- `get_modelplane_doc`: fetch the full Markdown of a single page.

The server URL is:

```plaintext
https://docs.modelplane.ai/mcp
```

{{< tabs >}}
{{< tab "Claude Code" >}}
```bash
claude mcp add --transport http modelplane-docs https://docs.modelplane.ai/mcp
```
{{< /tab >}}
{{< tab "Claude Desktop" >}}
Open Settings, go to Connectors, and choose **Add custom connector**. Name it `modelplane-docs`, enter the server URL above, and enable the connector when you start a conversation.
{{< /tab >}}
{{< tab "Cursor" >}}
Open the command palette, run **Cursor Settings: MCP**, and add a server to `mcp.json`:

```json
{
  "mcpServers": {
    "modelplane-docs": {
      "url": "https://docs.modelplane.ai/mcp"
    }
  }
}
```
{{< /tab >}}
{{< tab "VS Code" >}}
Create `.vscode/mcp.json` in your workspace:

```json
{
  "servers": {
    "modelplane-docs": {
      "type": "http",
      "url": "https://docs.modelplane.ai/mcp"
    }
  }
}
```
{{< /tab >}}
{{< tab "Other" >}}
Any MCP client that speaks the streamable HTTP transport can connect to the server URL directly. No authentication is required.
{{< /tab >}}
{{< /tabs >}}

The **Copy page** menu on every page also has **Connect to Cursor** and **Connect to VS Code** shortcuts that install the server in one click.

## Read pages as Markdown

Every page is also published as raw Markdown. Add `index.md` to any page URL:

```plaintext
https://docs.modelplane.ai/models/model-deployment/index.md
```

The **Copy page** control next to each title copies that Markdown to your clipboard, and **View as Markdown** opens it in the browser. Paste it into any assistant when you want to ground a question in a specific page.

## llms.txt

For tools that index a whole site, the docs publish the [`llms.txt`](https://llmstxt.org) format:

- [`llms.txt`](/llms.txt): a short index of every page with links and descriptions.
- [`llms-full.txt`](/llms-full.txt): every page concatenated into one Markdown file.

## Page menu reference

The **Copy page** menu next to each title has these actions:

| Action | What it does |
|---|---|
| Copy page | Copies the page as Markdown to your clipboard. |
| View as Markdown | Opens the page as raw Markdown. |
| Copy MCP Server | Copies the MCP server URL to your clipboard. |
| Connect to Cursor | Installs the MCP server in Cursor. |
| Connect to VS Code | Installs the MCP server in VS Code. |

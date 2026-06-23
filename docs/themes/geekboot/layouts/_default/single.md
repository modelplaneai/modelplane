{{- /* Raw-Markdown rendering of a docs page, served at page/index.md.
       Powers the "View as Markdown" action and feeds the docs MCP server.
       Normal pages use .RawContent (verbatim source); reference pages, generated
       from the CRD schema, render via the markdown-body partial, which also emits
       the lead description. */ -}}
{{- printf "# %s\n\n" .Title -}}
{{- if ne .Params.product "crd" }}{{ with .Description }}{{ printf "%s\n\n" . }}{{ end }}{{ end -}}
{{- with .OutputFormats.Get "html" }}{{ printf "Source: %s\n\n" .Permalink }}{{ end -}}
{{- /* Partials render through html/template, which escapes the body; htmlUnescape
       recovers the verbatim Markdown for this plain-text output. */ -}}
{{- partial "markdown-body.txt" . | htmlUnescape -}}

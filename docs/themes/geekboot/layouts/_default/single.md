{{- /* Raw-Markdown rendering of a docs page, served at page/index.md.
       Powers the "View as Markdown" action and feeds the docs MCP server.
       Uses .RawContent so the source Markdown is preserved verbatim. */ -}}
{{- printf "# %s\n\n" .Title -}}
{{- with .Description }}{{ printf "%s\n\n" . }}{{ end -}}
{{- with .OutputFormats.Get "html" }}{{ printf "Source: %s\n\n" .Permalink }}{{ end -}}
{{- .RawContent -}}

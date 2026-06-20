{{- /* Raw-Markdown rendering of a section landing page, served at section/index.md. */ -}}
{{- printf "# %s\n\n" .Title -}}
{{- with .Description }}{{ printf "%s\n\n" . }}{{ end -}}
{{- with .OutputFormats.Get "html" }}{{ printf "Source: %s\n\n" .Permalink }}{{ end -}}
{{- .RawContent -}}

/*
Copyright 2026 The Modelplane Authors.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

// Package web implements the Modelplane console HTTP server. It serves the
// embedded SPA frontend and proxies API requests to the Kubernetes API server
// and model inference endpoints.
package web

import (
	"context"
	"embed"
	"io/fs"
	"log/slog"
	"net/http"
	"strings"
	"time"
)

//go:embed all:static
var staticFS embed.FS

// An EndpointResolver resolves a ModelDeployment's inference endpoint URL from
// its namespace and name.
type EndpointResolver interface {
	ResolveEndpoint(ctx context.Context, ns, name string) (string, error)
}

// An EndpointResolverFn satisfies EndpointResolver using a plain function.
type EndpointResolverFn func(ctx context.Context, ns, name string) (string, error)

// ResolveEndpoint calls fn.
func (fn EndpointResolverFn) ResolveEndpoint(ctx context.Context, ns, name string) (string, error) {
	return fn(ctx, ns, name)
}

// An Option configures a Server.
type Option func(*Server)

// WithLogger sets the logger. Defaults to a no-op logger.
func WithLogger(l *slog.Logger) Option {
	return func(s *Server) { s.log = l }
}

// WithHTTPClient sets the HTTP client used to forward chat requests to
// inference endpoints. Defaults to http.DefaultClient.
func WithHTTPClient(c *http.Client) Option {
	return func(s *Server) { s.client = c }
}

// A Server serves the Modelplane console.
type Server struct {
	log    *slog.Logger
	client *http.Client

	kube http.Handler
	chat http.Handler
}

// NewServer returns a new Server. The host and transport configure the
// Kubernetes API proxy. The EndpointResolver is used by the chat proxy to look
// up ModelDeployment endpoint URLs.
func NewServer(host string, transport http.RoundTripper, er EndpointResolver, o ...Option) *Server {
	s := &Server{
		log:    slog.New(discardHandler{}),
		client: http.DefaultClient,
	}
	for _, fn := range o {
		fn(s)
	}

	s.kube = NewKubeProxy(s.log, host, transport)
	s.chat = NewChatProxy(s.log, er, s.client)
	return s
}

// Handler returns an http.Handler that routes requests to the appropriate
// backend. It serves:
//
//   - GET /healthz          — 200 OK
//   - /api/k8s/...          — Kubernetes API proxy
//   - /api/chat/{ns}/{name} — inference endpoint proxy
//   - everything else       — the embedded SPA
func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()

	mux.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	})

	mux.Handle("/api/k8s/", s.kube)
	mux.Handle("/api/chat/", s.chat)

	// Serve the embedded SPA. In development the static/ directory is empty —
	// use 'npm run dev' to serve the frontend with hot reload and proxy API
	// requests to this server.
	static, _ := fs.Sub(staticFS, "static")
	mux.Handle("/", SPAHandler(static))

	return mux
}

// SPAHandler serves a single-page app. It serves the requested file if it
// exists, otherwise falls back to index.html for client-side routing.
func SPAHandler(fsys fs.FS) http.Handler {
	fileServer := http.FileServer(http.FS(fsys))
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		// Try the requested path. fs.FS paths must not have a leading slash.
		p := strings.TrimPrefix(r.URL.Path, "/")
		if p == "" {
			p = "index.html"
		}
		if _, err := fs.Stat(fsys, p); err == nil {
			fileServer.ServeHTTP(w, r)
			return
		}

		// Fall back to index.html for client-side routes.
		r.URL.Path = "/"
		fileServer.ServeHTTP(w, r)
	})
}

// WithLogging wraps a handler with request logging.
func WithLogging(next http.Handler, log *slog.Logger) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		rec := &statusRecorder{ResponseWriter: w, status: http.StatusOK}
		next.ServeHTTP(rec, r)
		log.Info("request",
			"method", r.Method,
			"path", r.URL.RequestURI(),
			"status", rec.status,
			"duration", time.Since(start),
		)
	})
}

// statusRecorder wraps http.ResponseWriter to capture the status code.
type statusRecorder struct {
	http.ResponseWriter

	status int
}

func (sr *statusRecorder) WriteHeader(code int) {
	sr.status = code
	sr.ResponseWriter.WriteHeader(code)
}

// discardHandler is a slog.Handler that discards all log records.
type discardHandler struct{}

func (discardHandler) Enabled(context.Context, slog.Level) bool  { return false }
func (discardHandler) Handle(context.Context, slog.Record) error { return nil }
func (dh discardHandler) WithAttrs([]slog.Attr) slog.Handler     { return dh }
func (dh discardHandler) WithGroup(string) slog.Handler          { return dh }

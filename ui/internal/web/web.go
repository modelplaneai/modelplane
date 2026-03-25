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
	"embed"
	"fmt"
	"io/fs"
	"log/slog"
	"net/http"
	"time"

	"k8s.io/client-go/rest"
	"k8s.io/client-go/tools/clientcmd"
)

// Error strings.
const (
	errLoadConfig = "cannot load Kubernetes config"
	errTransport  = "cannot create Kubernetes transport"
)

//go:embed all:static
var staticFS embed.FS

// A Server serves the Modelplane console.
type Server struct {
	log  *slog.Logger
	kube http.Handler
	chat http.Handler
}

// NewServer returns a new Server. If kubeconfig is empty, in-cluster
// configuration is used.
func NewServer(log *slog.Logger, kubeconfig string) (*Server, error) {
	cfg, err := loadRESTConfig(kubeconfig)
	if err != nil {
		return nil, fmt.Errorf("%s: %w", errLoadConfig, err)
	}

	transport, err := rest.TransportFor(cfg)
	if err != nil {
		return nil, fmt.Errorf("%s: %w", errTransport, err)
	}

	return &Server{
		log:  log,
		kube: newKubeProxy(log, cfg.Host, transport),
		chat: newChatProxy(log, cfg),
	}, nil
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

	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	})

	mux.Handle("/api/k8s/", s.kube)
	mux.Handle("/api/chat/", s.chat)

	// Serve the embedded SPA. In development the static/ directory is empty —
	// use 'npm run dev' to serve the frontend with hot reload and proxy API
	// requests to this server.
	static, _ := fs.Sub(staticFS, "static")
	mux.Handle("GET /", spaHandler(static))

	return mux
}

// spaHandler serves a single-page app. It serves the requested file if it
// exists, otherwise falls back to index.html for client-side routing.
func spaHandler(fsys fs.FS) http.Handler {
	fileServer := http.FileServer(http.FS(fsys))
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		// Try the requested path.
		p := r.URL.Path
		if p == "/" {
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

func (r *statusRecorder) WriteHeader(code int) {
	r.status = code
	r.ResponseWriter.WriteHeader(code)
}

func loadRESTConfig(kubeconfig string) (*rest.Config, error) {
	if kubeconfig != "" {
		return clientcmd.BuildConfigFromFlags("", kubeconfig)
	}
	return rest.InClusterConfig()
}

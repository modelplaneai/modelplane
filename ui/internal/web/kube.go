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

package web

import (
	"log/slog"
	"net/http"
	"net/http/httputil"
	"strings"
)

const kubePrefix = "/api/k8s"

// NewKubeProxy returns a handler that strips the /api/k8s prefix and forwards
// the request to the Kubernetes API server. The supplied transport carries the
// cluster credentials (bearer token, client certs, etc.).
func NewKubeProxy(log *slog.Logger, host string, transport http.RoundTripper) http.Handler {
	// Trim any trailing slash and scheme prefix to get a bare host:port for
	// the URL rewriter.
	h := strings.TrimPrefix(strings.TrimPrefix(host, "https://"), "http://")

	return &httputil.ReverseProxy{
		Director: func(r *http.Request) {
			r.URL.Path = strings.TrimPrefix(r.URL.Path, kubePrefix)
			if r.URL.Path == "" {
				r.URL.Path = "/"
			}
			r.URL.Scheme = "https"
			r.URL.Host = h
			r.Host = h

			// Drop browser headers that kube-apiserver doesn't expect.
			r.Header.Del("Origin")
			r.Header.Del("Referer")
		},
		Transport: transport,
		ErrorHandler: func(w http.ResponseWriter, r *http.Request, err error) {
			log.Error("kube proxy", "path", r.URL.Path, "err", err)
			http.Error(w, "proxy error", http.StatusBadGateway)
		},
	}
}

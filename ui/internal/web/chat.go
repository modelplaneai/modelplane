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
	"io"
	"log/slog"
	"net/http"
	"strings"
)

// NewChatProxy returns a handler that resolves the ModelDeployment's endpoint
// URL and forwards the request body to it. The response is streamed back for
// SSE (token-by-token) display.
//
// Route: POST /api/chat/{namespace}/{name}.
func NewChatProxy(log *slog.Logger, er EndpointResolver, client *http.Client) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		ns, name, ok := parseChatPath(r.URL.Path)
		if !ok {
			http.Error(w, "cannot parse chat path, expected /api/chat/{namespace}/{name}", http.StatusBadRequest)
			return
		}

		endpoint, err := er.ResolveEndpoint(r.Context(), ns, name)
		if err != nil {
			log.Error("cannot resolve endpoint", "namespace", ns, "name", name, "err", err)
			http.Error(w, "cannot resolve endpoint", http.StatusBadGateway)
			return
		}
		if endpoint == "" {
			http.Error(w, "ModelDeployment has no endpoint URL", http.StatusServiceUnavailable)
			return
		}

		// Forward the request body to the inference endpoint.
		upstream, err := http.NewRequestWithContext(r.Context(), http.MethodPost, endpoint, r.Body)
		if err != nil {
			log.Error("cannot forward chat request", "endpoint", endpoint, "err", err)
			http.Error(w, "cannot forward chat request", http.StatusInternalServerError)
			return
		}
		upstream.Header.Set("Content-Type", "application/json")

		resp, err := client.Do(upstream)
		if err != nil {
			log.Error("cannot forward chat request", "endpoint", endpoint, "err", err)
			http.Error(w, "cannot forward chat request", http.StatusBadGateway)
			return
		}

		streamResponse(w, resp)
	})
}

// streamResponse copies an upstream HTTP response to w, flushing after each
// chunk for SSE streaming.
func streamResponse(w http.ResponseWriter, resp *http.Response) {
	defer resp.Body.Close() //nolint:errcheck // Best effort.

	for k, vs := range resp.Header {
		for _, v := range vs {
			w.Header().Add(k, v)
		}
	}
	w.WriteHeader(resp.StatusCode)

	if f, ok := w.(http.Flusher); ok {
		buf := make([]byte, 4096)
		for {
			n, err := resp.Body.Read(buf)
			if n > 0 {
				_, _ = w.Write(buf[:n])
				f.Flush()
			}
			if err != nil {
				break
			}
		}
	} else {
		_, _ = io.Copy(w, resp.Body)
	}
}

// parseChatPath extracts namespace and name from /api/chat/{namespace}/{name}.
func parseChatPath(path string) (ns, name string, ok bool) {
	path = strings.TrimPrefix(path, "/api/chat/")
	parts := strings.SplitN(path, "/", 2)
	if len(parts) != 2 || parts[0] == "" || parts[1] == "" {
		return "", "", false
	}
	return parts[0], parts[1], true
}

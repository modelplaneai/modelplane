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
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"strings"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/client-go/dynamic"
	"k8s.io/client-go/rest"
)

// Error strings.
const (
	errParseChatPath    = "cannot parse chat path, expected /api/chat/{namespace}/{name}"
	errGetDeployment    = "cannot get ModelDeployment"
	errNoEndpoint       = "ModelDeployment has no endpoint URL"
	errForwardChat      = "cannot forward chat request"
	errFmtBadChatMethod = "chat proxy only accepts POST, got %s"
)

var modelDeploymentGVR = schema.GroupVersionResource{
	Group:    "modelplane.ai",
	Version:  "v1alpha1",
	Resource: "modeldeployments",
}

// newChatProxy returns a handler that resolves the ModelDeployment's endpoint
// URL and forwards the request body to it. The response is streamed back for
// SSE (token-by-token) display.
//
// Route: POST /api/chat/{namespace}/{name}
func newChatProxy(log *slog.Logger, cfg *rest.Config) http.Handler {
	client := dynamic.NewForConfigOrDie(cfg)

	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, fmt.Sprintf(errFmtBadChatMethod, r.Method), http.StatusMethodNotAllowed)
			return
		}

		ns, name, ok := parseChatPath(r.URL.Path)
		if !ok {
			http.Error(w, errParseChatPath, http.StatusBadRequest)
			return
		}

		endpoint, err := resolveEndpoint(r.Context(), client, ns, name)
		if err != nil {
			log.Error(errGetDeployment, "namespace", ns, "name", name, "err", err)
			http.Error(w, errGetDeployment, http.StatusBadGateway)
			return
		}
		if endpoint == "" {
			http.Error(w, errNoEndpoint, http.StatusServiceUnavailable)
			return
		}

		// Forward the request body to the inference endpoint.
		upstream, err := http.NewRequestWithContext(r.Context(), http.MethodPost, endpoint, r.Body)
		if err != nil {
			log.Error(errForwardChat, "endpoint", endpoint, "err", err)
			http.Error(w, errForwardChat, http.StatusInternalServerError)
			return
		}
		upstream.Header.Set("Content-Type", "application/json")

		resp, err := http.DefaultClient.Do(upstream)
		if err != nil {
			log.Error(errForwardChat, "endpoint", endpoint, "err", err)
			http.Error(w, errForwardChat, http.StatusBadGateway)
			return
		}
		defer resp.Body.Close() //nolint:errcheck // Best effort.

		// Stream the response back to the browser. Copy all headers (including
		// Content-Type: text/event-stream for SSE).
		for k, vs := range resp.Header {
			for _, v := range vs {
				w.Header().Add(k, v)
			}
		}
		w.WriteHeader(resp.StatusCode)

		// Flush after each write for SSE streaming.
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
	})
}

// parseChatPath extracts namespace and name from /api/chat/{namespace}/{name}.
func parseChatPath(path string) (ns, name string, ok bool) {
	// Trim the prefix and split the remainder.
	path = strings.TrimPrefix(path, "/api/chat/")
	parts := strings.SplitN(path, "/", 2)
	if len(parts) != 2 || parts[0] == "" || parts[1] == "" {
		return "", "", false
	}
	return parts[0], parts[1], true
}

// resolveEndpoint reads a ModelDeployment and returns its status.endpoint.url.
func resolveEndpoint(ctx context.Context, client dynamic.Interface, ns, name string) (string, error) {
	md, err := client.Resource(modelDeploymentGVR).Namespace(ns).Get(ctx, name, metav1.GetOptions{})
	if err != nil {
		return "", fmt.Errorf("%s %s/%s: %w", errGetDeployment, ns, name, err)
	}

	url, _, _ := unstructured.NestedString(md.Object, "status", "endpoint", "url")
	return url, nil
}

// chatMessage is the minimal shape of an OpenAI chat request, used only for
// extracting the model name when logging.
type chatMessage struct {
	Model string `json:"model"`
}

// extractModel reads the model name from a chat request body without consuming
// it. Returns empty string on failure.
func extractModel(body io.Reader) string {
	var m chatMessage
	if err := json.NewDecoder(body).Decode(&m); err != nil {
		return ""
	}
	return m.Model
}

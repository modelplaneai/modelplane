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
	"net/http"
	"net/http/httptest"
	"testing"
	"testing/fstest"

	"github.com/google/go-cmp/cmp"
)

func TestSPAHandler(t *testing.T) {
	fsys := fstest.MapFS{
		"index.html":       {Data: []byte("<html>app</html>")},
		"assets/style.css": {Data: []byte("body{}")},
		"assets/app.js":    {Data: []byte("console.log('hi')")},
		"favicon.svg":      {Data: []byte("<svg/>")},
	}

	type args struct {
		path string
	}
	type want struct {
		code int
		body string
	}

	cases := map[string]struct {
		reason string
		args   args
		want   want
	}{
		"Root": {
			reason: "A request for / should serve index.html.",
			args:   args{path: "/"},
			want:   want{code: http.StatusOK, body: "<html>app</html>"},
		},
		"StaticAsset": {
			reason: "A request for an existing file should serve that file.",
			args:   args{path: "/assets/style.css"},
			want:   want{code: http.StatusOK, body: "body{}"},
		},
		"Favicon": {
			reason: "A request for favicon.svg should serve the favicon.",
			args:   args{path: "/favicon.svg"},
			want:   want{code: http.StatusOK, body: "<svg/>"},
		},
		"ClientRoute": {
			reason: "A request for a non-existent path should fall back to index.html for client-side routing.",
			args:   args{path: "/deployments/ml-team/qwen"},
			want:   want{code: http.StatusOK, body: "<html>app</html>"},
		},
	}

	for name, tc := range cases {
		t.Run(name, func(t *testing.T) {
			h := SPAHandler(fsys)
			req := httptest.NewRequest(http.MethodGet, tc.args.path, nil)
			rec := httptest.NewRecorder()

			h.ServeHTTP(rec, req)

			if diff := cmp.Diff(tc.want.code, rec.Code); diff != "" {
				t.Errorf("\n%s\nSPAHandler(...): -want code, +got code:\n%s", tc.reason, diff)
			}
			if diff := cmp.Diff(tc.want.body, rec.Body.String()); diff != "" {
				t.Errorf("\n%s\nSPAHandler(...): -want body, +got body:\n%s", tc.reason, diff)
			}
		})
	}
}

func TestHandler(t *testing.T) {
	type args struct {
		method string
		path   string
	}
	type want struct {
		code int
	}

	// Build a minimal server with stub handlers.
	s := NewServer(
		"https://127.0.0.1:6443",
		http.DefaultTransport,
		EndpointResolverFn(func(_ context.Context, _, _ string) (string, error) {
			return "", nil
		}),
	)
	h := s.Handler()

	cases := map[string]struct {
		reason string
		args   args
		want   want
	}{
		"Healthz": {
			reason: "GET /healthz should return 200.",
			args:   args{method: http.MethodGet, path: "/healthz"},
			want:   want{code: http.StatusOK},
		},
		"KubeAPI": {
			reason: "Requests to /api/k8s/ should be routed to the kube proxy.",
			args:   args{method: http.MethodGet, path: "/api/k8s/api/v1/namespaces"},
			// The kube proxy will fail (no real backend) but the route should match.
			want: want{code: http.StatusBadGateway},
		},
		"ChatAPI": {
			reason: "POST to /api/chat/ should be routed to the chat proxy.",
			args:   args{method: http.MethodPost, path: "/api/chat/ml-team/qwen"},
			// No endpoint URL resolved, so 503.
			want: want{code: http.StatusServiceUnavailable},
		},
		"SPAFallback": {
			reason: "Unknown GET paths should serve the SPA.",
			args:   args{method: http.MethodGet, path: "/models"},
			want:   want{code: http.StatusOK},
		},
	}

	for name, tc := range cases {
		t.Run(name, func(t *testing.T) {
			req := httptest.NewRequest(tc.args.method, tc.args.path, nil)
			rec := httptest.NewRecorder()

			h.ServeHTTP(rec, req)

			if diff := cmp.Diff(tc.want.code, rec.Code); diff != "" {
				t.Errorf("\n%s\nHandler(...): -want code, +got code:\n%s", tc.reason, diff)
			}
		})
	}
}

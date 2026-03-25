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
	"net/http/httptest"
	"testing"

	"github.com/google/go-cmp/cmp"
)

func TestNewKubeProxy(t *testing.T) {
	type args struct {
		method string
		path   string
	}
	type want struct {
		code int
		path string // The path the upstream sees.
		body string
	}

	cases := map[string]struct {
		reason   string
		args     args
		want     want
		upstream http.HandlerFunc
	}{
		"StripsPrefixAndForwards": {
			reason: "The /api/k8s prefix should be stripped and the request forwarded.",
			args:   args{method: http.MethodGet, path: "/api/k8s/apis/modelplane.ai/v1alpha1/clustermodels"},
			upstream: func(w http.ResponseWriter, r *http.Request) {
				_, _ = io.WriteString(w, r.URL.Path)
			},
			want: want{code: http.StatusOK, path: "/apis/modelplane.ai/v1alpha1/clustermodels", body: "/apis/modelplane.ai/v1alpha1/clustermodels"},
		},
		"RootPath": {
			reason: "A request to /api/k8s with no trailing path should forward to /.",
			args:   args{method: http.MethodGet, path: "/api/k8s"},
			upstream: func(w http.ResponseWriter, r *http.Request) {
				_, _ = io.WriteString(w, r.URL.Path)
			},
			want: want{code: http.StatusOK, body: "/"},
		},
		"DropsOriginHeader": {
			reason: "The Origin header should be removed before forwarding.",
			args:   args{method: http.MethodGet, path: "/api/k8s/api/v1"},
			upstream: func(w http.ResponseWriter, r *http.Request) {
				_, _ = io.WriteString(w, r.Header.Get("Origin"))
			},
			want: want{code: http.StatusOK, body: ""},
		},
	}

	for name, tc := range cases {
		t.Run(name, func(t *testing.T) {
			upstream := httptest.NewTLSServer(tc.upstream)
			defer upstream.Close()

			log := slog.New(discardHandler{})
			h := NewKubeProxy(log, upstream.URL, upstream.Client().Transport)

			req := httptest.NewRequest(tc.args.method, tc.args.path, nil)
			req.Header.Set("Origin", "http://localhost:5173")
			rec := httptest.NewRecorder()

			h.ServeHTTP(rec, req)

			if diff := cmp.Diff(tc.want.code, rec.Code); diff != "" {
				t.Errorf("\n%s\nNewKubeProxy(...): -want code, +got code:\n%s", tc.reason, diff)
			}
			if diff := cmp.Diff(tc.want.body, rec.Body.String()); diff != "" {
				t.Errorf("\n%s\nNewKubeProxy(...): -want body, +got body:\n%s", tc.reason, diff)
			}
		})
	}
}

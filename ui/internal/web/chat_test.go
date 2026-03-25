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
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/google/go-cmp/cmp"
)

var errBoom = errors.New("boom")

func TestParseChatPath(t *testing.T) {
	type want struct {
		ns   string
		name string
		ok   bool
	}

	cases := map[string]struct {
		reason string
		path   string
		want   want
	}{
		"ValidPath": {
			reason: "A well-formed path should return the namespace and name.",
			path:   "/api/chat/ml-team/qwen-demo",
			want:   want{ns: "ml-team", name: "qwen-demo", ok: true},
		},
		"MissingName": {
			reason: "A path with only a namespace should fail.",
			path:   "/api/chat/ml-team/",
			want:   want{ok: false},
		},
		"MissingNamespace": {
			reason: "A path with only the prefix should fail.",
			path:   "/api/chat/",
			want:   want{ok: false},
		},
		"Empty": {
			reason: "An empty path should fail.",
			path:   "",
			want:   want{ok: false},
		},
		"NameWithSlashes": {
			reason: "Everything after the first slash is the name, including further slashes.",
			path:   "/api/chat/ns/name/with/slashes",
			want:   want{ns: "ns", name: "name/with/slashes", ok: true},
		},
	}

	for name, tc := range cases {
		t.Run(name, func(t *testing.T) {
			ns, n, ok := parseChatPath(tc.path)

			if diff := cmp.Diff(tc.want.ok, ok); diff != "" {
				t.Errorf("\n%s\nparseChatPath(...): -want ok, +got ok:\n%s", tc.reason, diff)
			}
			if diff := cmp.Diff(tc.want.ns, ns); diff != "" {
				t.Errorf("\n%s\nparseChatPath(...): -want ns, +got ns:\n%s", tc.reason, diff)
			}
			if diff := cmp.Diff(tc.want.name, n); diff != "" {
				t.Errorf("\n%s\nparseChatPath(...): -want name, +got name:\n%s", tc.reason, diff)
			}
		})
	}
}

func TestNewChatProxy(t *testing.T) {
	type args struct {
		method string
		path   string
		body   string
	}
	type want struct {
		code int
		body string
	}

	cases := map[string]struct {
		reason   string
		resolver EndpointResolverFn
		args     args
		want     want
	}{
		"SuccessfulStream": {
			reason: "A valid POST should resolve the endpoint and stream the response.",
			resolver: EndpointResolverFn(func(_ context.Context, _, _ string) (string, error) {
				// The upstream URL is set per-case in the test body.
				return "", nil
			}),
			args: args{method: http.MethodPost, path: "/api/chat/ml-team/qwen", body: `{"model":"qwen"}`},
			want: want{code: http.StatusOK, body: `{"response":"hello"}`},
		},
		"BadPath": {
			reason: "A malformed path should return 400 Bad Request.",
			resolver: EndpointResolverFn(func(_ context.Context, _, _ string) (string, error) {
				return "http://unused", nil
			}),
			args: args{method: http.MethodPost, path: "/api/chat/"},
			want: want{code: http.StatusBadRequest},
		},
		"ResolveError": {
			reason: "A resolver error should return 502 Bad Gateway.",
			resolver: EndpointResolverFn(func(_ context.Context, _, _ string) (string, error) {
				return "", errBoom
			}),
			args: args{method: http.MethodPost, path: "/api/chat/ml-team/qwen", body: `{}`},
			want: want{code: http.StatusBadGateway},
		},
		"NoEndpoint": {
			reason: "An empty endpoint URL should return 503 Service Unavailable.",
			resolver: EndpointResolverFn(func(_ context.Context, _, _ string) (string, error) {
				return "", nil
			}),
			args: args{method: http.MethodPost, path: "/api/chat/ml-team/qwen", body: `{}`},
			want: want{code: http.StatusServiceUnavailable},
		},
	}

	for name, tc := range cases {
		t.Run(name, func(t *testing.T) {
			// For the success case, spin up a fake upstream that returns a
			// known response.
			if name == "SuccessfulStream" {
				upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
					w.Header().Set("Content-Type", "application/json")
					_, _ = io.WriteString(w, `{"response":"hello"}`)
				}))
				defer upstream.Close()

				tc.resolver = EndpointResolverFn(func(_ context.Context, _, _ string) (string, error) {
					return upstream.URL, nil
				})
			}

			log := slog.New(discardHandler{})
			h := NewChatProxy(log, tc.resolver, http.DefaultClient)

			var body io.Reader
			if tc.args.body != "" {
				body = strings.NewReader(tc.args.body)
			}
			req := httptest.NewRequest(tc.args.method, tc.args.path, body)
			rec := httptest.NewRecorder()

			h.ServeHTTP(rec, req)

			if diff := cmp.Diff(tc.want.code, rec.Code); diff != "" {
				t.Errorf("\n%s\nNewChatProxy(...): -want code, +got code:\n%s", tc.reason, diff)
			}
			if tc.want.body != "" {
				if diff := cmp.Diff(tc.want.body, rec.Body.String()); diff != "" {
					t.Errorf("\n%s\nNewChatProxy(...): -want body, +got body:\n%s", tc.reason, diff)
				}
			}
		})
	}
}

func TestStreamResponse(t *testing.T) {
	type want struct {
		code int
		body string
	}

	cases := map[string]struct {
		reason string
		resp   *http.Response
		want   want
	}{
		"CopiesBodyAndStatus": {
			reason: "The upstream status code and body should be copied to the response.",
			resp: &http.Response{
				StatusCode: http.StatusOK,
				Header:     http.Header{"Content-Type": {"text/event-stream"}},
				Body:       io.NopCloser(strings.NewReader("data: hello\n\n")),
			},
			want: want{code: http.StatusOK, body: "data: hello\n\n"},
		},
	}

	for name, tc := range cases {
		t.Run(name, func(t *testing.T) {
			rec := httptest.NewRecorder()
			streamResponse(rec, tc.resp)

			if diff := cmp.Diff(tc.want.code, rec.Code); diff != "" {
				t.Errorf("\n%s\nstreamResponse(...): -want code, +got code:\n%s", tc.reason, diff)
			}
			if diff := cmp.Diff(tc.want.body, rec.Body.String()); diff != "" {
				t.Errorf("\n%s\nstreamResponse(...): -want body, +got body:\n%s", tc.reason, diff)
			}
		})
	}
}

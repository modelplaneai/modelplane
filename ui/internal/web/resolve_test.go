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
	"testing"

	"github.com/google/go-cmp/cmp"
	"github.com/google/go-cmp/cmp/cmpopts"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"
	dynamicfake "k8s.io/client-go/dynamic/fake"
)

func TestNewKubernetesEndpointResolver(t *testing.T) {
	type args struct {
		ns   string
		name string
	}
	type want struct {
		url string
		err error
	}

	cases := map[string]struct {
		reason  string
		objects []runtime.Object
		args    args
		want    want
	}{
		"ResolvesEndpoint": {
			reason: "The resolver should return the endpoint URL from the ModelDeployment's status.",
			objects: []runtime.Object{
				&unstructured.Unstructured{
					Object: map[string]any{
						"apiVersion": "modelplane.ai/v1alpha1",
						"kind":       "ModelDeployment",
						"metadata": map[string]any{
							"name":      "qwen-demo",
							"namespace": "ml-team",
						},
						"status": map[string]any{
							"endpoint": map[string]any{
								"url": "http://10.0.0.50/v1/chat/completions",
							},
						},
					},
				},
			},
			args: args{ns: "ml-team", name: "qwen-demo"},
			want: want{url: "http://10.0.0.50/v1/chat/completions"},
		},
		"NoEndpointInStatus": {
			reason: "A ModelDeployment without a status endpoint should return an empty URL.",
			objects: []runtime.Object{
				&unstructured.Unstructured{
					Object: map[string]any{
						"apiVersion": "modelplane.ai/v1alpha1",
						"kind":       "ModelDeployment",
						"metadata": map[string]any{
							"name":      "qwen-demo",
							"namespace": "ml-team",
						},
					},
				},
			},
			args: args{ns: "ml-team", name: "qwen-demo"},
			want: want{url: ""},
		},
		"NotFound": {
			reason: "A missing ModelDeployment should return an error.",
			args:   args{ns: "ml-team", name: "does-not-exist"},
			want:   want{err: cmpopts.AnyError},
		},
	}

	for name, tc := range cases {
		t.Run(name, func(t *testing.T) {
			scheme := runtime.NewScheme()
			client := dynamicfake.NewSimpleDynamicClient(scheme, tc.objects...)

			resolve := NewKubernetesEndpointResolver(client)
			got, err := resolve.ResolveEndpoint(context.Background(), tc.args.ns, tc.args.name)

			if diff := cmp.Diff(tc.want.err, err, cmpopts.EquateErrors()); diff != "" {
				t.Errorf("\n%s\nResolveEndpoint(...): -want error, +got error:\n%s", tc.reason, diff)
			}
			if diff := cmp.Diff(tc.want.url, got); diff != "" {
				t.Errorf("\n%s\nResolveEndpoint(...): -want url, +got url:\n%s", tc.reason, diff)
			}
		})
	}
}

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
	"fmt"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/client-go/dynamic"
)

// NewKubernetesEndpointResolver returns an EndpointResolver that reads a
// ModelDeployment's status.endpoint.url from the Kubernetes API.
func NewKubernetesEndpointResolver(client dynamic.Interface) EndpointResolverFn {
	gvr := schema.GroupVersionResource{
		Group:    "modelplane.ai",
		Version:  "v1alpha1",
		Resource: "modeldeployments",
	}

	return func(ctx context.Context, ns, name string) (string, error) {
		md, err := client.Resource(gvr).Namespace(ns).Get(ctx, name, metav1.GetOptions{})
		if err != nil {
			return "", fmt.Errorf("cannot get ModelDeployment %s/%s: %w", ns, name, err)
		}

		url, _, _ := unstructured.NestedString(md.Object, "status", "endpoint", "url")
		return url, nil
	}
}

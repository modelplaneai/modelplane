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

// Command proxy serves the Modelplane web console and proxies API requests to
// the Kubernetes API server and model inference endpoints.
package main

import (
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"time"

	"github.com/alecthomas/kong"
	"k8s.io/client-go/dynamic"
	"k8s.io/client-go/rest"
	"k8s.io/client-go/tools/clientcmd"

	"github.com/modelplaneai/modelplane/ui/internal/web"
)

type cli struct {
	Addr       string `default:":8080"              help:"Address to listen on."`
	Kubeconfig string `default:""                   help:"Path to kubeconfig file (omit for in-cluster)." optional:""`
	Verbose    bool   `help:"Enable debug logging." short:"v"`
}

func (c *cli) Run() error {
	level := slog.LevelInfo
	if c.Verbose {
		level = slog.LevelDebug
	}
	log := slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: level}))

	cfg, err := loadRESTConfig(c.Kubeconfig)
	if err != nil {
		return fmt.Errorf("cannot load Kubernetes config: %w", err)
	}

	transport, err := rest.TransportFor(cfg)
	if err != nil {
		return fmt.Errorf("cannot create Kubernetes transport: %w", err)
	}

	resolver := web.NewKubernetesEndpointResolver(dynamic.NewForConfigOrDie(cfg))

	s := web.NewServer(cfg.Host, transport, resolver, web.WithLogger(log))

	srv := &http.Server{
		Addr:              c.Addr,
		Handler:           web.WithLogging(s.Handler(), log),
		ReadHeaderTimeout: 10 * time.Second,
		WriteTimeout:      120 * time.Second, // Long for watch and chat streaming.
	}

	log.Info("listening", "addr", c.Addr)
	return srv.ListenAndServe()
}

func main() {
	c := &cli{}
	ctx := kong.Parse(c,
		kong.Name("modelplane-ui"),
		kong.Description("Modelplane web console."),
		kong.UsageOnError(),
	)
	ctx.FatalIfErrorf(c.Run())
}

func loadRESTConfig(kubeconfig string) (*rest.Config, error) {
	if kubeconfig != "" {
		return clientcmd.BuildConfigFromFlags("", kubeconfig)
	}
	return rest.InClusterConfig()
}

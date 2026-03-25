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
	"flag"
	"log/slog"
	"net/http"
	"os"
	"time"

	"github.com/modelplaneai/modelplane/ui/internal/web"
)

func main() {
	var (
		addr       = flag.String("addr", ":8080", "Listen address")
		kubeconfig = flag.String("kubeconfig", "", "Path to kubeconfig file (omit for in-cluster)")
	)
	flag.Parse()

	log := slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelInfo}))

	s, err := web.NewServer(log, *kubeconfig)
	if err != nil {
		log.Error("cannot create server", "err", err)
		os.Exit(1)
	}

	srv := &http.Server{
		Addr:              *addr,
		Handler:           web.WithLogging(s.Handler(), log),
		ReadHeaderTimeout: 10 * time.Second,
		WriteTimeout:      120 * time.Second, // Long for watch and chat streaming.
	}

	log.Info("listening", "addr", *addr)
	if err := srv.ListenAndServe(); err != nil {
		log.Error("cannot serve", "err", err)
		os.Exit(1)
	}
}

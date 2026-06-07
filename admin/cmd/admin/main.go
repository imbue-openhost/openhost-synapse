package main

import (
	"log"
	"net/http"
	"os"

	"github.com/imbue-openhost/openhost-synapse/admin/internal/config"
	"github.com/imbue-openhost/openhost-synapse/admin/internal/handlers"
)

func main() {
	dataDir := os.Getenv("OPENHOST_APP_DATA_DIR")
	if dataDir == "" {
		dataDir = "/data"
	}

	cfg := config.New(dataDir)

	mux := handlers.NewMux(cfg)

	port := os.Getenv("ADMIN_PORT")
	if port == "" {
		port = "8009"
	}

	addr := "127.0.0.1:" + port
	log.Printf("OpenHost Synapse admin server listening on %s", addr)
	if err := http.ListenAndServe(addr, mux); err != nil {
		log.Fatalf("admin server: %v", err)
	}
}

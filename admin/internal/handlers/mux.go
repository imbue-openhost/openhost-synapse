// Package handlers wires up all HTTP routes for the admin UI.
package handlers

import (
	"net/http"

	"github.com/imbue-openhost/openhost-synapse/admin/internal/config"
	"github.com/imbue-openhost/openhost-synapse/admin/internal/synapse"
)

// NewMux builds and returns the HTTP mux.
func NewMux(cfg *config.Config) http.Handler {
	client := synapse.NewClient(cfg.DataDir())
	h := &handler{cfg: cfg, syn: client}

	mux := http.NewServeMux()

	// Landing / dashboard
	mux.HandleFunc("GET /_openhost/admin", h.dashboard)
	mux.HandleFunc("GET /_openhost/admin/", h.dashboard)

	// Root redirect -> admin
	mux.HandleFunc("GET /", func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/" {
			http.Redirect(w, r, "/_openhost/admin", http.StatusFound)
			return
		}
		http.NotFound(w, r)
	})

	// Settings
	mux.HandleFunc("GET /_openhost/admin/settings", h.settingsPage)
	mux.HandleFunc("POST /_openhost/admin/settings", h.settingsSave)

	// Users
	mux.HandleFunc("GET /_openhost/admin/users", h.usersPage)
	mux.HandleFunc("GET /_openhost/admin/users/search", h.usersSearch)
	mux.HandleFunc("GET /_openhost/admin/users/{userID}", h.userDetail)
	mux.HandleFunc("POST /_openhost/admin/users/{userID}/deactivate", h.userDeactivate)
	mux.HandleFunc("POST /_openhost/admin/users/{userID}/promote", h.userPromote)
	mux.HandleFunc("POST /_openhost/admin/users/{userID}/reset-password", h.userResetPassword)
	mux.HandleFunc("POST /_openhost/admin/users/{userID}/delete-media", h.userDeleteMedia)
	mux.HandleFunc("GET /_openhost/admin/users/new", h.userNewForm)
	mux.HandleFunc("POST /_openhost/admin/users/new", h.userCreate)

	// Rooms
	mux.HandleFunc("GET /_openhost/admin/rooms", h.roomsPage)
	mux.HandleFunc("GET /_openhost/admin/rooms/search", h.roomsSearch)
	mux.HandleFunc("POST /_openhost/admin/rooms/{roomID}/delete", h.roomDelete)
	mux.HandleFunc("POST /_openhost/admin/rooms/{roomID}/make-admin", h.roomMakeAdmin)

	// Registration tokens
	mux.HandleFunc("GET /_openhost/admin/tokens", h.tokensPage)
	mux.HandleFunc("POST /_openhost/admin/tokens/create", h.tokenCreate)
	mux.HandleFunc("POST /_openhost/admin/tokens/{token}/delete", h.tokenDelete)

	// HTMX partials
	mux.HandleFunc("GET /_openhost/admin/partials/stats", h.statsPartial)
	mux.HandleFunc("GET /_openhost/admin/partials/status", h.statusPartial)

	return mux
}

type handler struct {
	cfg *config.Config
	syn *synapse.Client
}

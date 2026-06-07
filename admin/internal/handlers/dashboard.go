package handlers

import (
	"html/template"
	"net/http"
)

var dashboardTpl = template.Must(template.New("dashboard").Parse(`
<div class="stat-grid" id="stats-grid"
     hx-get="/_openhost/admin/partials/stats"
     hx-trigger="load, every 30s"
     hx-swap="innerHTML">
  <div class="stat-card">
    <div class="stat-label">Total Users</div>
    <div class="stat-value"><span class="spinner"></span></div>
  </div>
</div>

<div class="card">
  <div class="card-title">Quick Actions</div>
  <div style="display:flex;gap:0.75rem;flex-wrap:wrap;">
    <a href="/_openhost/admin/users/new" class="btn btn-primary">+ New User</a>
    <a href="/_openhost/admin/tokens" class="btn btn-secondary">+ Reg. Token</a>
    <a href="/_openhost/admin/settings" class="btn btn-secondary">Settings</a>
  </div>
</div>

<div class="card">
  <div class="card-title">Recent Users</div>
  <div hx-get="/_openhost/admin/users/search?limit=10"
       hx-trigger="load"
       hx-swap="innerHTML">
    <span class="spinner"></span>
  </div>
</div>
`))

func (h *handler) dashboard(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/" && r.URL.Path != "/_openhost/admin" && r.URL.Path != "/_openhost/admin/" {
		http.NotFound(w, r)
		return
	}
	renderPage(w, "Dashboard", "dashboard", dashboardTpl, nil)
}

var statsTpl = template.Must(template.New("stats").Funcs(template.FuncMap{}).Parse(`
{{if .Initializing}}
<div class="stat-card" style="grid-column:1/-1;">
  <div class="stat-label">Status</div>
  <div class="stat-value" style="font-size:1rem;color:var(--warning)">Initializing…</div>
  <p style="font-size:0.8rem;color:var(--text-faint);margin-top:0.5rem;">Synapse is starting up. The admin panel will be fully functional in a moment.</p>
</div>
{{else if .Error}}
<div class="stat-card">
  <div class="stat-label">Status</div>
  <div class="stat-value" style="font-size:1rem;color:var(--error)">Unavailable</div>
</div>
{{else}}
<div class="stat-card">
  <div class="stat-label">Total Users</div>
  <div class="stat-value">{{.TotalUsers}}</div>
</div>
<div class="stat-card">
  <div class="stat-label">Total Rooms</div>
  <div class="stat-value">{{.TotalRooms}}</div>
</div>
<div class="stat-card">
  <div class="stat-label">Version</div>
  <div class="stat-value" style="font-size:0.875rem;padding-top:0.5rem;">{{.Version}}</div>
</div>
{{end}}
`))

func (h *handler) statsPartial(w http.ResponseWriter, r *http.Request) {
	type statsData struct {
		TotalUsers  int
		TotalRooms  int
		Version     string
		Error       bool
		Initializing bool
	}

	// Check if Synapse is reachable yet
	if !h.syn.IsReady() {
		renderPartial(w, statsTpl, statsData{Initializing: true})
		return
	}

	version, _ := h.syn.ServerVersion()
	users, err := h.syn.ListUsers(0, 1, true, "")

	data := statsData{Version: version}
	if err != nil || users == nil {
		data.Error = true
	} else {
		data.TotalUsers = users.Total
	}

	rooms, err := h.syn.ListRooms(0, 1, "")
	if err == nil && rooms != nil {
		data.TotalRooms = rooms.TotalRooms
	}

	renderPartial(w, statsTpl, data)
}

var statusTpl = template.Must(template.New("status").Parse(`{{.}}`))

func (h *handler) statusPartial(w http.ResponseWriter, r *http.Request) {
	if h.syn.IsReady() {
		renderPartial(w, statusTpl, "Online")
	} else {
		renderPartial(w, statusTpl, "Offline")
	}
}

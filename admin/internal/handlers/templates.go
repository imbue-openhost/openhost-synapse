package handlers

import (
	"bytes"
	"html/template"
	"net/http"
	"time"
)

// baseLayout is the shared HTML shell for all full-page responses.
// HTMX partial responses use the inner templates directly.
const baseLayout = `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{{.Title}} — Synapse Admin</title>
  <script src="https://unpkg.com/htmx.org@1.9.12/dist/htmx.min.js"></script>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    :root {
      --bg: #0f1117;
      --surface: #1e2130;
      --surface2: #252a3a;
      --border: #2d3348;
      --accent: #6366f1;
      --accent-hover: #4f46e5;
      --text: #e2e8f0;
      --text-muted: #94a3b8;
      --text-faint: #64748b;
      --success: #4ade80;
      --success-bg: #052e16;
      --success-border: #166534;
      --warning: #fbbf24;
      --warning-bg: #1c1003;
      --warning-border: #92400e;
      --error: #f87171;
      --error-bg: #1c0505;
      --error-border: #991b1b;
      --red: #ef4444;
      --red-hover: #dc2626;
      --sidebar-w: 220px;
    }
    html, body { height: 100%; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: var(--bg);
      color: var(--text);
      display: flex;
      min-height: 100vh;
    }
    /* Sidebar */
    .sidebar {
      width: var(--sidebar-w);
      background: var(--surface);
      border-right: 1px solid var(--border);
      display: flex;
      flex-direction: column;
      flex-shrink: 0;
      position: fixed;
      top: 0; left: 0; bottom: 0;
      z-index: 10;
    }
    .sidebar-brand {
      padding: 1.25rem 1.25rem 1rem;
      border-bottom: 1px solid var(--border);
    }
    .sidebar-brand h1 {
      font-size: 1rem;
      font-weight: 600;
      color: #f8fafc;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .sidebar-brand .version {
      font-size: 0.7rem;
      color: var(--text-faint);
      margin-top: 0.15rem;
    }
    nav { flex: 1; padding: 0.75rem 0; overflow-y: auto; }
    nav a {
      display: flex;
      align-items: center;
      gap: 0.6rem;
      padding: 0.6rem 1.25rem;
      color: var(--text-muted);
      text-decoration: none;
      font-size: 0.875rem;
      border-radius: 0;
      transition: color 0.15s, background 0.15s;
    }
    nav a:hover, nav a.active {
      color: var(--text);
      background: var(--surface2);
    }
    nav a.active { border-left: 3px solid var(--accent); }
    nav .nav-section {
      font-size: 0.7rem;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--text-faint);
      padding: 0.75rem 1.25rem 0.25rem;
    }
    /* Main content */
    .main {
      margin-left: var(--sidebar-w);
      flex: 1;
      display: flex;
      flex-direction: column;
      min-height: 100vh;
    }
    .topbar {
      background: var(--surface);
      border-bottom: 1px solid var(--border);
      padding: 0.875rem 1.75rem;
      display: flex;
      align-items: center;
      justify-content: space-between;
    }
    .topbar h2 { font-size: 1.1rem; font-weight: 600; color: #f8fafc; }
    .topbar .status-dot {
      display: inline-block;
      width: 8px; height: 8px;
      border-radius: 50%;
      background: var(--success);
      margin-right: 0.4rem;
    }
    .topbar .status-dot.offline { background: var(--error); }
    .topbar .status-label { font-size: 0.8rem; color: var(--text-muted); }
    .content { padding: 1.75rem; flex: 1; }
    /* Cards */
    .card {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 0.75rem;
      padding: 1.5rem;
      margin-bottom: 1rem;
    }
    .card-title {
      font-size: 0.9375rem;
      font-weight: 600;
      color: #f1f5f9;
      margin-bottom: 1rem;
    }
    /* Stat grid */
    .stat-grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
      gap: 1rem;
      margin-bottom: 1.5rem;
    }
    .stat-card {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 0.6rem;
      padding: 1.25rem;
    }
    .stat-card .stat-label { font-size: 0.75rem; color: var(--text-faint); margin-bottom: 0.3rem; }
    .stat-card .stat-value { font-size: 1.75rem; font-weight: 700; color: #f8fafc; }
    /* Forms */
    .form-group { margin-bottom: 1.25rem; }
    label {
      display: block;
      font-size: 0.875rem;
      font-weight: 500;
      color: #cbd5e1;
      margin-bottom: 0.4rem;
    }
    input[type=text], input[type=password], input[type=number], input[type=email], select, textarea {
      width: 100%;
      background: var(--surface2);
      border: 1px solid var(--border);
      border-radius: 0.4rem;
      padding: 0.6rem 0.75rem;
      color: var(--text);
      font-size: 0.875rem;
      outline: none;
      transition: border-color 0.15s;
    }
    input:focus, select:focus, textarea:focus { border-color: var(--accent); }
    /* Toggle */
    .toggle-label { position: relative; display: inline-block; width: 48px; height: 26px; flex-shrink: 0; }
    .toggle-label input { opacity: 0; width: 0; height: 0; }
    .slider {
      position: absolute; inset: 0;
      background: #374151; border-radius: 26px; cursor: pointer; transition: background 0.2s;
    }
    .slider::before {
      content: ""; position: absolute;
      width: 18px; height: 18px; left: 4px; top: 4px;
      background: #fff; border-radius: 50%; transition: transform 0.2s;
    }
    input:checked + .slider { background: var(--accent); }
    input:checked + .slider::before { transform: translateX(22px); }
    .setting-row {
      display: flex; align-items: center; justify-content: space-between; gap: 1rem;
    }
    .setting-info h3 { font-size: 0.9375rem; font-weight: 500; color: #f1f5f9; margin-bottom: 0.25rem; }
    .setting-info p { font-size: 0.8rem; color: var(--text-faint); line-height: 1.4; }
    /* Buttons */
    .btn {
      display: inline-flex; align-items: center; gap: 0.4rem;
      padding: 0.55rem 1rem;
      border: none; border-radius: 0.4rem;
      font-size: 0.875rem; font-weight: 500;
      cursor: pointer; text-decoration: none;
      transition: background 0.15s, opacity 0.15s;
    }
    .btn-primary { background: var(--accent); color: #fff; }
    .btn-primary:hover { background: var(--accent-hover); }
    .btn-danger { background: var(--red); color: #fff; }
    .btn-danger:hover { background: var(--red-hover); }
    .btn-secondary { background: var(--surface2); color: var(--text); border: 1px solid var(--border); }
    .btn-secondary:hover { background: var(--border); }
    .btn-sm { padding: 0.35rem 0.65rem; font-size: 0.8rem; }
    .btn:disabled { opacity: 0.5; cursor: not-allowed; }
    /* Alerts */
    .alert {
      padding: 0.75rem 1rem; border-radius: 0.5rem;
      font-size: 0.875rem; margin-bottom: 1.25rem;
    }
    .alert-success { background: var(--success-bg); border: 1px solid var(--success-border); color: var(--success); }
    .alert-warning { background: var(--warning-bg); border: 1px solid var(--warning-border); color: var(--warning); }
    .alert-error { background: var(--error-bg); border: 1px solid var(--error-border); color: var(--error); }
    /* Table */
    .table-wrap { overflow-x: auto; }
    table { width: 100%; border-collapse: collapse; font-size: 0.875rem; }
    thead th {
      text-align: left; padding: 0.75rem 1rem;
      border-bottom: 1px solid var(--border);
      color: var(--text-faint); font-weight: 500; font-size: 0.8rem;
      text-transform: uppercase; letter-spacing: 0.05em;
    }
    tbody td {
      padding: 0.75rem 1rem;
      border-bottom: 1px solid var(--border);
      color: var(--text-muted);
      vertical-align: middle;
    }
    tbody tr:last-child td { border-bottom: none; }
    tbody tr:hover td { background: var(--surface2); }
    .badge {
      display: inline-block; padding: 0.2rem 0.5rem;
      border-radius: 0.25rem; font-size: 0.75rem; font-weight: 500;
    }
    .badge-green { background: #052e16; color: #4ade80; border: 1px solid #166534; }
    .badge-red { background: #1c0505; color: #f87171; border: 1px solid #991b1b; }
    .badge-blue { background: #0c1a4a; color: #93c5fd; border: 1px solid #1d4ed8; }
    .badge-yellow { background: #1c1003; color: #fbbf24; border: 1px solid #92400e; }
    .badge-gray { background: var(--surface2); color: var(--text-faint); border: 1px solid var(--border); }
    /* Pagination */
    .pagination { display: flex; gap: 0.5rem; align-items: center; margin-top: 1rem; }
    /* Search box */
    .search-box {
      display: flex; gap: 0.75rem; align-items: center; margin-bottom: 1rem;
    }
    .search-box input { max-width: 320px; }
    /* HTMX indicator */
    .htmx-indicator { display: none; }
    .htmx-request .htmx-indicator { display: inline; }
    .htmx-request.htmx-indicator { display: inline; }
    /* Spinner */
    @keyframes spin { to { transform: rotate(360deg); } }
    .spinner {
      display: inline-block;
      width: 1rem; height: 1rem;
      border: 2px solid var(--border);
      border-top-color: var(--accent);
      border-radius: 50%;
      animation: spin 0.6s linear infinite;
    }
    /* Modal-like overlay for confirm dialogs */
    details summary { cursor: pointer; color: var(--text-muted); font-size: 0.8rem; }
    details[open] summary { color: var(--text); margin-bottom: 0.75rem; }
    /* Responsive tweaks */
    @media (max-width: 640px) {
      .sidebar { display: none; }
      .main { margin-left: 0; }
    }
  </style>
</head>
<body>
  <aside class="sidebar">
    <div class="sidebar-brand">
      <h1>Synapse Admin</h1>
      <div class="version" hx-get="/_openhost/admin/partials/status" hx-trigger="load" hx-swap="innerHTML">
        <span class="spinner"></span>
      </div>
    </div>
    <nav>
      <div class="nav-section">Overview</div>
      <a href="/_openhost/admin" {{if eq .ActiveNav "dashboard"}}class="active"{{end}}>
        &#9632; Dashboard
      </a>
      <div class="nav-section">Management</div>
      <a href="/_openhost/admin/users" {{if eq .ActiveNav "users"}}class="active"{{end}}>
        &#128100; Users
      </a>
      <a href="/_openhost/admin/rooms" {{if eq .ActiveNav "rooms"}}class="active"{{end}}>
        &#128172; Rooms
      </a>
      <a href="/_openhost/admin/tokens" {{if eq .ActiveNav "tokens"}}class="active"{{end}}>
        &#128273; Reg. Tokens
      </a>
      <div class="nav-section">Configuration</div>
      <a href="/_openhost/admin/settings" {{if eq .ActiveNav "settings"}}class="active"{{end}}>
        &#9881; Settings
      </a>
    </nav>
  </aside>
  <div class="main">
    <div class="topbar">
      <h2>{{.Title}}</h2>
      <div>
        <span class="status-dot" id="status-dot"></span>
        <span class="status-label" id="status-label">Checking…</span>
      </div>
    </div>
    <div class="content">
      {{.Body}}
    </div>
  </div>
  <script>
    // Poll Synapse status
    async function checkStatus() {
      try {
        const r = await fetch('/_openhost/admin/partials/status');
        const text = await r.text();
        const dot = document.getElementById('status-dot');
        const lbl = document.getElementById('status-label');
        if (text.includes('Online')) {
          dot.className = 'status-dot';
          lbl.textContent = 'Online';
        } else {
          dot.className = 'status-dot offline';
          lbl.textContent = 'Offline';
        }
      } catch {}
    }
    checkStatus();
    setInterval(checkStatus, 15000);
  </script>
</body>
</html>`

type pageData struct {
	Title     string
	ActiveNav string
	Body      template.HTML
}

func renderPage(w http.ResponseWriter, title, activeNav string, bodyTpl *template.Template, data interface{}) {
	var buf bytes.Buffer
	if err := bodyTpl.Execute(&buf, data); err != nil {
		http.Error(w, "template error: "+err.Error(), http.StatusInternalServerError)
		return
	}

	base := template.Must(template.New("base").Parse(baseLayout))
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	if err := base.Execute(w, pageData{
		Title:     title,
		ActiveNav: activeNav,
		Body:      template.HTML(buf.String()), //nolint:gosec
	}); err != nil {
		http.Error(w, "layout error: "+err.Error(), http.StatusInternalServerError)
	}
}

func renderPartial(w http.ResponseWriter, tpl *template.Template, data interface{}) {
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	if err := tpl.Execute(w, data); err != nil {
		http.Error(w, "partial error: "+err.Error(), http.StatusInternalServerError)
	}
}

func formatTime(ts int64) string {
	if ts == 0 {
		return "—"
	}
	t := time.Unix(ts/1000, 0)
	return t.Format("2006-01-02 15:04")
}

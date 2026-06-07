package handlers

import (
	"fmt"
	"html/template"
	"log"
	"net/http"
	"strconv"

	"github.com/imbue-openhost/openhost-synapse/admin/internal/config"
)

var settingsTpl = template.Must(template.New("settings").Parse(`
{{if .Message}}
<div class="alert alert-success" id="settings-alert">{{.Message}}</div>
{{end}}
{{if .Warning}}
<div class="alert alert-warning" id="settings-alert">{{.Warning}}</div>
{{end}}

<form method="POST" action="/_openhost/admin/settings"
      hx-post="/_openhost/admin/settings"
      hx-target="#settings-result"
      hx-swap="innerHTML">

  <div id="settings-result"></div>

  <!-- Registration -->
  <div class="card">
    <div class="card-title">Registration</div>

    <div class="setting-row" style="margin-bottom:1rem;">
      <div class="setting-info">
        <h3>Open Registration</h3>
        <p>Allow anyone to create an account without an invitation or token.</p>
      </div>
      <label class="toggle-label">
        <input type="checkbox" name="open_registration" value="1" {{if .S.OpenRegistration}}checked{{end}}>
        <span class="slider"></span>
      </label>
    </div>

    <div class="form-group">
      <label>Minimum Password Length</label>
      <input type="number" name="password_min_length" value="{{.S.PasswordMinLength}}" min="4" max="128" style="max-width:120px;">
    </div>

    <div class="setting-row" style="margin-bottom:1rem;">
      <div class="setting-info">
        <h3>Require Digit in Password</h3>
        <p>Passwords must include at least one numeric character.</p>
      </div>
      <label class="toggle-label">
        <input type="checkbox" name="password_require_digit" value="1" {{if .S.PasswordRequireDigit}}checked{{end}}>
        <span class="slider"></span>
      </label>
    </div>

    <div class="setting-row">
      <div class="setting-info">
        <h3>Require Symbol in Password</h3>
        <p>Passwords must include at least one punctuation/symbol character.</p>
      </div>
      <label class="toggle-label">
        <input type="checkbox" name="password_require_symbol" value="1" {{if .S.PasswordRequireSymbol}}checked{{end}}>
        <span class="slider"></span>
      </label>
    </div>
  </div>

  <!-- Federation -->
  <div class="card">
    <div class="card-title">Federation</div>

    <div class="setting-row">
      <div class="setting-info">
        <h3>Enable Federation</h3>
        <p>Allow this server to communicate with other Matrix servers across the network. Restart not required — applied immediately via SIGHUP.</p>
      </div>
      <label class="toggle-label">
        <input type="checkbox" name="federation_enabled" value="1" {{if .S.FederationEnabled}}checked{{end}}>
        <span class="slider"></span>
      </label>
    </div>
  </div>

  <!-- Rooms -->
  <div class="card">
    <div class="card-title">Rooms</div>

    <div class="setting-row">
      <div class="setting-info">
        <h3>Allow Public Rooms</h3>
        <p>Allow rooms to be listed in the public room directory.</p>
      </div>
      <label class="toggle-label">
        <input type="checkbox" name="allow_public_rooms" value="1" {{if .S.AllowPublicRooms}}checked{{end}}>
        <span class="slider"></span>
      </label>
    </div>
  </div>

  <!-- Rate Limits -->
  <div class="card">
    <div class="card-title">Rate Limits</div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:1rem;">
      <div class="form-group">
        <label>Login attempts / second</label>
        <input type="number" name="rc_login_per_second" value="{{printf "%.0f" .S.RCLoginPerSecond}}" min="0.1" max="100" step="0.1">
      </div>
      <div class="form-group">
        <label>Login burst count</label>
        <input type="number" name="rc_login_burst" value="{{.S.RCLoginBurst}}" min="1" max="1000">
      </div>
    </div>
  </div>

  <!-- Media -->
  <div class="card">
    <div class="card-title">Media</div>
    <div class="form-group">
      <label>Max upload size (MB)</label>
      <input type="number" name="max_upload_size_mb" value="{{.S.MaxUploadSizeMB}}" min="1" max="2048" style="max-width:160px;">
    </div>
  </div>

  <button type="submit" class="btn btn-primary" style="margin-top:0.5rem;">
    Save &amp; Apply
    <span class="htmx-indicator spinner"></span>
  </button>
</form>
`))

var settingsResultTpl = template.Must(template.New("settings-result").Parse(`
{{if .Error}}
<div class="alert alert-error">{{.Error}}</div>
{{else if .Warning}}
<div class="alert alert-warning">{{.Warning}}</div>
{{else}}
<div class="alert alert-success">Settings saved and applied — no restart needed.</div>
{{end}}
`))

type settingsPageData struct {
	S       *config.Settings
	Message string
	Warning string
}

func (h *handler) settingsPage(w http.ResponseWriter, r *http.Request) {
	s, err := h.cfg.Load()
	if err != nil {
		http.Error(w, "load settings: "+err.Error(), http.StatusInternalServerError)
		return
	}
	renderPage(w, "Settings", "settings", settingsTpl, settingsPageData{S: s})
}

func (h *handler) settingsSave(w http.ResponseWriter, r *http.Request) {
	if err := r.ParseForm(); err != nil {
		renderPartial(w, settingsResultTpl, map[string]string{"Error": "parse form: " + err.Error()})
		return
	}

	perSecond, _ := strconv.ParseFloat(r.FormValue("rc_login_per_second"), 64)
	if perSecond <= 0 {
		perSecond = 10
	}
	burst, _ := strconv.Atoi(r.FormValue("rc_login_burst"))
	if burst <= 0 {
		burst = 50
	}
	uploadMB, _ := strconv.Atoi(r.FormValue("max_upload_size_mb"))
	if uploadMB <= 0 {
		uploadMB = 50
	}
	minPwLen, _ := strconv.Atoi(r.FormValue("password_min_length"))
	if minPwLen <= 0 {
		minPwLen = 8
	}

	s := &config.Settings{
		OpenRegistration:      r.FormValue("open_registration") == "1",
		FederationEnabled:     r.FormValue("federation_enabled") == "1",
		AllowPublicRooms:      r.FormValue("allow_public_rooms") == "1",
		RCLoginPerSecond:      perSecond,
		RCLoginBurst:          burst,
		MaxUploadSizeMB:       uploadMB,
		PasswordMinLength:     minPwLen,
		PasswordRequireDigit:  r.FormValue("password_require_digit") == "1",
		PasswordRequireSymbol: r.FormValue("password_require_symbol") == "1",
	}

	if err := h.cfg.Save(s); err != nil {
		log.Printf("settings save: %v", err)
		renderPartial(w, settingsResultTpl, map[string]string{"Error": fmt.Sprintf("Save failed: %v", err)})
		return
	}

	// Reload Synapse so changes take effect without a restart
	if err := reloadSynapse(); err != nil {
		log.Printf("reload synapse: %v", err)
		renderPartial(w, settingsResultTpl, map[string]string{
			"Warning": "Settings saved to disk, but could not signal Synapse to reload: " + err.Error() + ". Changes will apply on next restart.",
		})
		return
	}

	// For HTMX requests, return just the result snippet
	if r.Header.Get("HX-Request") == "true" {
		renderPartial(w, settingsResultTpl, map[string]interface{}{})
		return
	}

	// Full-page fallback
	renderPage(w, "Settings", "settings", settingsTpl, settingsPageData{
		S:       s,
		Message: "Settings saved and applied — no restart needed.",
	})
}

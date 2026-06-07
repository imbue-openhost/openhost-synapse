package handlers

import (
	"fmt"
	"html/template"
	"net/http"
	"strconv"
	"time"

	"github.com/imbue-openhost/openhost-synapse/admin/internal/synapse"
)

// tokenFuncMap is shared by all token templates to avoid duplicating helper functions.
var tokenFuncMap = template.FuncMap{
	"formatExpiry": func(ts *int64) string {
		if ts == nil {
			return "Never"
		}
		return time.Unix(*ts/1000, 0).Format("2006-01-02 15:04")
	},
	"derefInt": func(p *int) string {
		if p == nil {
			return "∞"
		}
		return strconv.Itoa(*p)
	},
}

var tokensPageTpl = template.Must(template.New("tokens-page").Funcs(tokenFuncMap).Parse(`
<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:1rem;">
  <p style="color:var(--text-muted);font-size:0.875rem;">
    Registration tokens let specific people create accounts even when open registration is disabled.
  </p>
</div>

<div class="card" style="max-width:480px;margin-bottom:1.5rem;">
  <div class="card-title">Create Token</div>
  <div id="token-create-result"></div>
  <form hx-post="/_openhost/admin/tokens/create"
        hx-target="#token-list" hx-swap="innerHTML"
        hx-on::after-request="this.reset()">
    <div class="form-group">
      <label>Token (leave blank to auto-generate)</label>
      <input type="text" name="token" placeholder="my-invite-token">
    </div>
    <div class="form-group">
      <label>Max uses (leave blank for unlimited)</label>
      <input type="number" name="uses_allowed" placeholder="1" min="1">
    </div>
    <div class="form-group">
      <label>Expiry (leave blank for no expiry)</label>
      <input type="datetime-local" name="expiry">
    </div>
    <button type="submit" class="btn btn-primary btn-sm">Create Token</button>
  </form>
</div>

<div class="card">
  <div class="card-title">Active Tokens</div>
  <div id="token-list"
       hx-get="/_openhost/admin/tokens"
       hx-trigger="load"
       hx-swap="innerHTML">
    <span class="spinner"></span>
  </div>
</div>
`))

var tokenListTpl = template.Must(template.New("token-list").Funcs(tokenFuncMap).Parse(`
{{if .Error}}
<div class="alert alert-error">{{.Error}}</div>
{{else}}
<div id="token-action-result"></div>
<div class="table-wrap">
  <table>
    <thead>
      <tr>
        <th>Token</th>
        <th>Uses Allowed</th>
        <th>Pending</th>
        <th>Completed</th>
        <th>Expires</th>
        <th>Actions</th>
      </tr>
    </thead>
    <tbody>
      {{range .Tokens}}
      <tr>
        <td><code style="font-size:0.8rem;color:var(--accent);">{{.Token}}</code></td>
        <td>{{derefInt .UsesAllowed}}</td>
        <td>{{.Pending}}</td>
        <td>{{.Completed}}</td>
        <td>{{formatExpiry .ExpiryTime}}</td>
        <td>
          <button class="btn btn-danger btn-sm"
            hx-post="/_openhost/admin/tokens/{{.Token}}/delete"
            hx-target="#token-action-result" hx-swap="innerHTML"
            hx-confirm="Delete token {{.Token}}?">
            Delete
          </button>
        </td>
      </tr>
      {{else}}
      <tr><td colspan="6" style="text-align:center;color:var(--text-faint);padding:2rem;">No tokens.</td></tr>
      {{end}}
    </tbody>
  </table>
</div>
{{end}}
`))

func (h *handler) tokensPage(w http.ResponseWriter, r *http.Request) {
	// If this is a full-page GET, render the page shell with inline token list
	if r.Header.Get("HX-Request") != "true" {
		renderPage(w, "Registration Tokens", "tokens", tokensPageTpl, nil)
		return
	}
	// HTMX hit on /tokens: return token list partial
	h.tokenList(w, r)
}

func (h *handler) tokenList(w http.ResponseWriter, r *http.Request) {
	type listData struct {
		Tokens []synapse.RegistrationToken
		Error  string
	}
	tokens, err := h.syn.ListRegistrationTokens()
	if err != nil {
		renderPartial(w, tokenListTpl, listData{Error: err.Error()})
		return
	}
	renderPartial(w, tokenListTpl, listData{Tokens: tokens})
}

func (h *handler) tokenCreate(w http.ResponseWriter, r *http.Request) {
	_ = r.ParseForm()

	tokenStr := r.FormValue("token")
	usesStr := r.FormValue("uses_allowed")
	expiryStr := r.FormValue("expiry")

	var usesAllowed *int
	if usesStr != "" {
		u, err := strconv.Atoi(usesStr)
		if err == nil && u > 0 {
			usesAllowed = &u
		}
	}

	var expiryMs *int64
	if expiryStr != "" {
		t, err := time.ParseInLocation("2006-01-02T15:04", expiryStr, time.Local)
		if err == nil {
			ms := t.UnixMilli()
			expiryMs = &ms
		}
	}

	type listData struct {
		Tokens []synapse.RegistrationToken
		Error  string
	}

	_, err := h.syn.CreateRegistrationToken(tokenStr, usesAllowed, expiryMs)
	if err != nil {
		renderPartial(w, tokenListTpl, listData{Error: fmt.Sprintf("Create failed: %v", err)})
		return
	}

	// Return refreshed list
	tokens, err := h.syn.ListRegistrationTokens()
	if err != nil {
		renderPartial(w, tokenListTpl, listData{Error: err.Error()})
		return
	}
	renderPartial(w, tokenListTpl, listData{Tokens: tokens})
}

func (h *handler) tokenDelete(w http.ResponseWriter, r *http.Request) {
	token := r.PathValue("token")

	type listData struct {
		Tokens []synapse.RegistrationToken
		Error  string
	}

	if err := h.syn.DeleteRegistrationToken(token); err != nil {
		renderPartial(w, tokenListTpl, listData{Error: fmt.Sprintf("Delete failed: %v", err)})
		return
	}

	tokens, err := h.syn.ListRegistrationTokens()
	if err != nil {
		renderPartial(w, tokenListTpl, listData{Error: err.Error()})
		return
	}
	renderPartial(w, tokenListTpl, listData{Tokens: tokens})
}

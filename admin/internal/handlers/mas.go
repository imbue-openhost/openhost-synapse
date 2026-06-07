package handlers

import (
	"html/template"
	"io"
	"net/http"
	"time"
)

// masStatusTpl is a partial for the MAS status widget on the dashboard.
var masStatusTpl = template.Must(template.New("mas-status").Parse(`
{{if .Online}}
<div class="alert alert-success" style="margin-bottom:0;">
  MAS (matrix-authentication-service) is <strong>online</strong> — OIDC/OAuth2 enabled.
  <a href="/_mas/admin/" style="color:var(--success);margin-left:0.5rem;">Open MAS Admin →</a>
</div>
{{else}}
<div class="alert alert-warning" style="margin-bottom:0;">
  MAS (matrix-authentication-service) is <strong>offline</strong> or not configured.
  Authentication falls back to Synapse's built-in password auth.
</div>
{{end}}
`))

func (h *handler) masStatusPartial(w http.ResponseWriter, r *http.Request) {
	online := isMASOnline()
	renderPartial(w, masStatusTpl, map[string]bool{"Online": online})
}

// isMASOnline checks if the MAS server is responding on localhost:8080.
func isMASOnline() bool {
	client := &http.Client{Timeout: 2 * time.Second}
	resp, err := client.Get("http://127.0.0.1:8080/health")
	if err != nil {
		return false
	}
	io.Copy(io.Discard, resp.Body) //nolint:errcheck
	resp.Body.Close()
	return resp.StatusCode == 200
}

package handlers

import (
	"fmt"
	"html/template"
	"log"
	"net/http"
	"strconv"
)

var usersPageTpl = template.Must(template.New("users-page").Parse(`
<div class="search-box">
  <input type="text" id="user-search" name="q" placeholder="Search by username…"
    hx-get="/_openhost/admin/users/search"
    hx-target="#user-table"
    hx-trigger="keyup changed delay:300ms, search"
    hx-include="[name='q']"
    style="max-width:320px;">
  <span class="htmx-indicator spinner"></span>
  <a href="/_openhost/admin/users/new" class="btn btn-primary btn-sm">+ New User</a>
</div>
<div id="user-table"
     hx-get="/_openhost/admin/users/search"
     hx-trigger="load"
     hx-swap="innerHTML">
  <span class="spinner"></span>
</div>
`))

var userTableTpl = template.Must(template.New("user-table").Funcs(template.FuncMap{
	"formatTime": formatTime,
}).Parse(`
{{if .Error}}
<div class="alert alert-error">{{.Error}}</div>
{{else}}
<div class="table-wrap">
  <table>
    <thead>
      <tr>
        <th>User ID</th>
        <th>Display Name</th>
        <th>Role</th>
        <th>Status</th>
        <th>Created</th>
        <th>Actions</th>
      </tr>
    </thead>
    <tbody>
      {{range .Users}}
      <tr>
        <td><a href="/_openhost/admin/users/{{.Name}}" style="color:var(--accent);text-decoration:none;">{{.Name}}</a></td>
        <td>{{if .DisplayName}}{{.DisplayName}}{{else}}<span style="color:var(--text-faint)">—</span>{{end}}</td>
        <td>
          {{if .Admin}}<span class="badge badge-blue">Admin</span>
          {{else if .GuestAccount}}<span class="badge badge-gray">Guest</span>
          {{else}}<span class="badge badge-green">User</span>{{end}}
        </td>
        <td>
          {{if .Deactivated}}<span class="badge badge-red">Deactivated</span>
          {{else if .ShadowBanned}}<span class="badge badge-yellow">Shadow Banned</span>
          {{else}}<span class="badge badge-green">Active</span>{{end}}
        </td>
        <td>{{formatTime .CreationTs}}</td>
        <td>
          <a href="/_openhost/admin/users/{{.Name}}" class="btn btn-secondary btn-sm">View</a>
        </td>
      </tr>
      {{else}}
      <tr><td colspan="6" style="text-align:center;color:var(--text-faint);padding:2rem;">No users found.</td></tr>
      {{end}}
    </tbody>
  </table>
</div>
{{if .NextToken}}
<div class="pagination">
  <button class="btn btn-secondary btn-sm"
    hx-get="/_openhost/admin/users/search?from={{.NextFrom}}&q={{.Query}}"
    hx-target="#user-table" hx-swap="innerHTML">
    Next →
  </button>
</div>
{{end}}
<div style="color:var(--text-faint);font-size:0.8rem;margin-top:0.5rem;">
  Showing {{len .Users}} of {{.Total}} users
</div>
{{end}}
`))

func (h *handler) usersPage(w http.ResponseWriter, r *http.Request) {
	renderPage(w, "Users", "users", usersPageTpl, nil)
}

func (h *handler) usersSearch(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query().Get("q")
	fromStr := r.URL.Query().Get("from")
	from, _ := strconv.Atoi(fromStr)
	limit := 25
	if limitStr := r.URL.Query().Get("limit"); limitStr != "" {
		if n, err := strconv.Atoi(limitStr); err == nil && n > 0 && n <= 100 {
			limit = n
		}
	}

	list, err := h.syn.ListUsers(from, limit, true, q)

	type tableData struct {
		Users     interface{}
		NextToken string
		NextFrom  int
		Total     int
		Query     string
		Error     string
	}

	if err != nil {
		renderPartial(w, userTableTpl, tableData{Error: err.Error()})
		return
	}

	renderPartial(w, userTableTpl, tableData{
		Users:     list.Users,
		NextToken: list.NextToken,
		NextFrom:  from + limit,
		Total:     list.Total,
		Query:     q,
	})
}

var userDetailTpl = template.Must(template.New("user-detail").Funcs(template.FuncMap{
	"formatTime": formatTime,
}).Parse(`
{{if .Error}}
<div class="alert alert-error">{{.Error}}</div>
{{else}}
<div style="display:flex;gap:1rem;margin-bottom:1rem;flex-wrap:wrap;">
  <a href="/_openhost/admin/users" class="btn btn-secondary btn-sm">← Back</a>
</div>

<div id="user-action-result"></div>

<div class="card">
  <div class="card-title">Account Info</div>
  <table style="width:auto;font-size:0.9rem;">
    <tr><td style="color:var(--text-faint);padding:0.4rem 1rem 0.4rem 0;">User ID</td><td>{{.User.Name}}</td></tr>
    <tr><td style="color:var(--text-faint);padding:0.4rem 1rem 0.4rem 0;">Display Name</td><td>{{if .User.DisplayName}}{{.User.DisplayName}}{{else}}—{{end}}</td></tr>
    <tr><td style="color:var(--text-faint);padding:0.4rem 1rem 0.4rem 0;">Admin</td><td>{{if .User.Admin}}<span class="badge badge-blue">Yes</span>{{else}}<span class="badge badge-gray">No</span>{{end}}</td></tr>
    <tr><td style="color:var(--text-faint);padding:0.4rem 1rem 0.4rem 0;">Status</td><td>{{if .User.Deactivated}}<span class="badge badge-red">Deactivated</span>{{else}}<span class="badge badge-green">Active</span>{{end}}</td></tr>
    <tr><td style="color:var(--text-faint);padding:0.4rem 1rem 0.4rem 0;">Guest</td><td>{{if .User.GuestAccount}}Yes{{else}}No{{end}}</td></tr>
    <tr><td style="color:var(--text-faint);padding:0.4rem 1rem 0.4rem 0;">Shadow Banned</td><td>{{if .User.ShadowBanned}}<span class="badge badge-yellow">Yes</span>{{else}}No{{end}}</td></tr>
    <tr><td style="color:var(--text-faint);padding:0.4rem 1rem 0.4rem 0;">Created</td><td>{{formatTime .User.CreationTs}}</td></tr>
  </table>
</div>

<div style="display:grid;grid-template-columns:1fr 1fr;gap:1rem;flex-wrap:wrap;">

  <!-- Promote/Demote -->
  <div class="card">
    <div class="card-title">Admin Role</div>
    {{if .User.Admin}}
    <p style="font-size:0.875rem;color:var(--text-muted);margin-bottom:1rem;">This user is currently an admin.</p>
    <button class="btn btn-secondary btn-sm"
      hx-post="/_openhost/admin/users/{{.User.Name}}/promote?admin=false"
      hx-target="#user-action-result" hx-swap="innerHTML">
      Remove Admin
    </button>
    {{else}}
    <p style="font-size:0.875rem;color:var(--text-muted);margin-bottom:1rem;">This user is not an admin.</p>
    <button class="btn btn-primary btn-sm"
      hx-post="/_openhost/admin/users/{{.User.Name}}/promote?admin=true"
      hx-target="#user-action-result" hx-swap="innerHTML">
      Make Admin
    </button>
    {{end}}
  </div>

  <!-- Reset password -->
  <div class="card">
    <div class="card-title">Reset Password</div>
    <form hx-post="/_openhost/admin/users/{{.User.Name}}/reset-password"
          hx-target="#user-action-result" hx-swap="innerHTML">
      <div class="form-group">
        <input type="password" name="new_password" placeholder="New password" required minlength="8">
      </div>
      <label style="font-size:0.8rem;display:flex;align-items:center;gap:0.4rem;margin-bottom:0.75rem;">
        <input type="checkbox" name="logout_all" value="1"> Log out all devices
      </label>
      <button type="submit" class="btn btn-secondary btn-sm">Reset Password</button>
    </form>
  </div>

  <!-- Delete media -->
  <div class="card">
    <div class="card-title">Media</div>
    <p style="font-size:0.875rem;color:var(--text-muted);margin-bottom:1rem;">Delete all media uploaded by this user.</p>
    <button class="btn btn-danger btn-sm"
      hx-post="/_openhost/admin/users/{{.User.Name}}/delete-media"
      hx-target="#user-action-result" hx-swap="innerHTML"
      hx-confirm="Delete all media for {{.User.Name}}?">
      Delete All Media
    </button>
  </div>

  <!-- Deactivate -->
  <div class="card">
    <div class="card-title">Deactivate Account</div>
    {{if .User.Deactivated}}
    <p style="font-size:0.875rem;color:var(--text-muted);">This account is already deactivated.</p>
    {{else}}
    <p style="font-size:0.875rem;color:var(--text-muted);margin-bottom:1rem;">Permanently deactivate this user account.</p>
    <form hx-post="/_openhost/admin/users/{{.User.Name}}/deactivate"
          hx-target="#user-action-result" hx-swap="innerHTML"
          hx-confirm="Deactivate {{.User.Name}}? This cannot be undone.">
      <label style="font-size:0.8rem;display:flex;align-items:center;gap:0.4rem;margin-bottom:0.75rem;">
        <input type="checkbox" name="erase" value="1"> Erase all user data (GDPR)
      </label>
      <button type="submit" class="btn btn-danger btn-sm">Deactivate User</button>
    </form>
    {{end}}
  </div>

</div>
{{end}}
`))

func (h *handler) userDetail(w http.ResponseWriter, r *http.Request) {
	userID := r.PathValue("userID")

	type detailData struct {
		User  interface{}
		Error string
	}

	user, err := h.syn.GetUser(userID)
	if err != nil {
		renderPage(w, "User: "+userID, "users", userDetailTpl, detailData{Error: err.Error()})
		return
	}

	renderPage(w, "User: "+userID, "users", userDetailTpl, detailData{User: user})
}

var actionResultTpl = template.Must(template.New("action-result").Parse(`
{{if .Error}}
<div class="alert alert-error">{{.Error}}</div>
{{else}}
<div class="alert alert-success">{{.Message}}</div>
{{end}}
`))

func (h *handler) userDeactivate(w http.ResponseWriter, r *http.Request) {
	userID := r.PathValue("userID")
	_ = r.ParseForm()
	erase := r.FormValue("erase") == "1"

	type result struct{ Error, Message string }
	if err := h.syn.DeactivateUser(userID, erase); err != nil {
		renderPartial(w, actionResultTpl, result{Error: fmt.Sprintf("Deactivate failed: %v", err)})
		return
	}
	renderPartial(w, actionResultTpl, result{Message: "User deactivated successfully."})
}

func (h *handler) userPromote(w http.ResponseWriter, r *http.Request) {
	userID := r.PathValue("userID")
	admin := r.URL.Query().Get("admin") != "false"

	type result struct{ Error, Message string }
	if err := h.syn.PromoteUser(userID, admin); err != nil {
		renderPartial(w, actionResultTpl, result{Error: fmt.Sprintf("Failed: %v", err)})
		return
	}
	msg := "Admin privileges granted."
	if !admin {
		msg = "Admin privileges removed."
	}
	renderPartial(w, actionResultTpl, result{Message: msg})
}

func (h *handler) userResetPassword(w http.ResponseWriter, r *http.Request) {
	userID := r.PathValue("userID")
	_ = r.ParseForm()
	newPw := r.FormValue("new_password")
	logoutAll := r.FormValue("logout_all") == "1"

	type result struct{ Error, Message string }
	if newPw == "" {
		renderPartial(w, actionResultTpl, result{Error: "Password cannot be empty."})
		return
	}
	if err := h.syn.ResetUserPassword(userID, newPw, logoutAll); err != nil {
		renderPartial(w, actionResultTpl, result{Error: fmt.Sprintf("Reset failed: %v", err)})
		return
	}
	renderPartial(w, actionResultTpl, result{Message: "Password reset successfully."})
}

func (h *handler) userDeleteMedia(w http.ResponseWriter, r *http.Request) {
	userID := r.PathValue("userID")

	type result struct{ Error, Message string }
	n, err := h.syn.DeleteUserMedia(userID)
	if err != nil {
		renderPartial(w, actionResultTpl, result{Error: fmt.Sprintf("Delete media failed: %v", err)})
		return
	}
	renderPartial(w, actionResultTpl, result{Message: fmt.Sprintf("Deleted %d media items.", n)})
}

var userNewTpl = template.Must(template.New("user-new").Parse(`
<div style="margin-bottom:1rem;">
  <a href="/_openhost/admin/users" class="btn btn-secondary btn-sm">← Back</a>
</div>

<div class="card" style="max-width:560px;">
  <div class="card-title">Create New User</div>
  <div id="create-result"></div>
  <form hx-post="/_openhost/admin/users/new"
        hx-target="#create-result" hx-swap="innerHTML">
    <div class="form-group">
      <label>User ID (e.g. @alice:example.com)</label>
      <input type="text" name="user_id" placeholder="@alice:example.com" required>
    </div>
    <div class="form-group">
      <label>Password</label>
      <input type="password" name="password" required minlength="8">
    </div>
    <div class="form-group">
      <label>Display Name (optional)</label>
      <input type="text" name="display_name" placeholder="Alice">
    </div>
    <label style="display:flex;align-items:center;gap:0.5rem;margin-bottom:1rem;font-size:0.875rem;">
      <input type="checkbox" name="admin" value="1">
      Make server admin
    </label>
    <button type="submit" class="btn btn-primary">Create User</button>
  </form>
</div>
`))

func (h *handler) userNewForm(w http.ResponseWriter, r *http.Request) {
	renderPage(w, "New User", "users", userNewTpl, nil)
}

func (h *handler) userCreate(w http.ResponseWriter, r *http.Request) {
	_ = r.ParseForm()
	userID := r.FormValue("user_id")
	password := r.FormValue("password")
	displayName := r.FormValue("display_name")
	isAdmin := r.FormValue("admin") == "1"

	type result struct{ Error, Message string }

	if userID == "" || password == "" {
		renderPartial(w, actionResultTpl, result{Error: "User ID and password are required."})
		return
	}

	body := map[string]interface{}{
		"password": password,
		"admin":    isAdmin,
	}
	if displayName != "" {
		body["displayname"] = displayName
	}

	if err := h.syn.CreateOrUpdateUser(userID, body); err != nil {
		log.Printf("create user %s: %v", userID, err)
		renderPartial(w, actionResultTpl, result{Error: fmt.Sprintf("Create failed: %v", err)})
		return
	}
	renderPartial(w, actionResultTpl, result{Message: fmt.Sprintf("User %s created successfully.", userID)})
}

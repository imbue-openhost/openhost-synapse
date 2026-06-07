package handlers

import (
	"fmt"
	"html/template"
	"net/http"
	"strconv"
)

var roomsPageTpl = template.Must(template.New("rooms-page").Parse(`
<div class="search-box">
  <input type="text" name="q" placeholder="Search rooms…"
    hx-get="/_openhost/admin/rooms/search"
    hx-target="#room-table"
    hx-trigger="keyup changed delay:300ms"
    hx-include="[name='q']"
    style="max-width:320px;">
  <span class="htmx-indicator spinner"></span>
</div>
<div id="room-table"
     hx-get="/_openhost/admin/rooms/search"
     hx-trigger="load"
     hx-swap="innerHTML">
  <span class="spinner"></span>
</div>
`))

var roomTableTpl = template.Must(template.New("room-table").Parse(`
{{if .Error}}
<div class="alert alert-error">{{.Error}}</div>
{{else}}
<div id="room-action-result"></div>
<div class="table-wrap">
  <table>
    <thead>
      <tr>
        <th>Room ID</th>
        <th>Name / Alias</th>
        <th>Members</th>
        <th>Local</th>
        <th>Encrypted</th>
        <th>Public</th>
        <th>Actions</th>
      </tr>
    </thead>
    <tbody>
      {{range .Rooms}}
      <tr>
        <td style="font-size:0.75rem;font-family:monospace;color:var(--text-faint);">{{.RoomID}}</td>
        <td>
          {{if .Name}}{{.Name}}{{else if .CanonicalAlias}}{{.CanonicalAlias}}{{else}}<span style="color:var(--text-faint)">—</span>{{end}}
        </td>
        <td>{{.JoinedMembers}}</td>
        <td>{{.JoinedLocalMembers}}</td>
        <td>{{if .Encryption}}<span class="badge badge-green">E2EE</span>{{else}}<span class="badge badge-gray">None</span>{{end}}</td>
        <td>{{if .Public}}<span class="badge badge-blue">Public</span>{{else}}<span class="badge badge-gray">Private</span>{{end}}</td>
        <td>
          <button class="btn btn-danger btn-sm"
            hx-post="/_openhost/admin/rooms/{{.RoomID}}/delete?purge=true&block=false"
            hx-target="#room-action-result" hx-swap="innerHTML"
            hx-confirm="Delete room {{.RoomID}}? This will purge all events.">
            Delete
          </button>
        </td>
      </tr>
      {{else}}
      <tr><td colspan="7" style="text-align:center;color:var(--text-faint);padding:2rem;">No rooms found.</td></tr>
      {{end}}
    </tbody>
  </table>
</div>
{{if .NextBatch}}
<div class="pagination">
  <button class="btn btn-secondary btn-sm"
    hx-get="/_openhost/admin/rooms/search?from={{.NextFrom}}&q={{.Query}}"
    hx-target="#room-table" hx-swap="innerHTML">
    Next →
  </button>
</div>
{{end}}
<div style="color:var(--text-faint);font-size:0.8rem;margin-top:0.5rem;">
  Showing {{len .Rooms}} of {{.TotalRooms}} rooms
</div>
{{end}}
`))

func (h *handler) roomsPage(w http.ResponseWriter, r *http.Request) {
	renderPage(w, "Rooms", "rooms", roomsPageTpl, nil)
}

func (h *handler) roomsSearch(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query().Get("q")
	fromStr := r.URL.Query().Get("from")
	from, _ := strconv.Atoi(fromStr)
	limit := 25

	type tableData struct {
		Rooms      interface{}
		NextBatch  string
		NextFrom   int
		TotalRooms int
		Query      string
		Error      string
	}

	list, err := h.syn.ListRooms(from, limit, q)
	if err != nil {
		renderPartial(w, roomTableTpl, tableData{Error: err.Error()})
		return
	}

	renderPartial(w, roomTableTpl, tableData{
		Rooms:      list.Rooms,
		NextBatch:  list.NextBatch,
		NextFrom:   from + limit,
		TotalRooms: list.TotalRooms,
		Query:      q,
	})
}

func (h *handler) roomDelete(w http.ResponseWriter, r *http.Request) {
	roomID := r.PathValue("roomID")
	purge := r.URL.Query().Get("purge") != "false"
	block := r.URL.Query().Get("block") == "true"

	type result struct{ Error, Message string }
	_, err := h.syn.DeleteRoom(roomID, block, purge)
	if err != nil {
		renderPartial(w, actionResultTpl, result{Error: fmt.Sprintf("Delete room failed: %v", err)})
		return
	}
	renderPartial(w, actionResultTpl, result{Message: fmt.Sprintf("Room %s deleted.", roomID)})
}

func (h *handler) roomMakeAdmin(w http.ResponseWriter, r *http.Request) {
	roomID := r.PathValue("roomID")
	_ = r.ParseForm()
	userID := r.FormValue("user_id")

	type result struct{ Error, Message string }
	if err := h.syn.MakeRoomAdmin(roomID, userID); err != nil {
		renderPartial(w, actionResultTpl, result{Error: fmt.Sprintf("Make room admin failed: %v", err)})
		return
	}
	renderPartial(w, actionResultTpl, result{Message: fmt.Sprintf("%s is now admin of %s.", userID, roomID)})
}

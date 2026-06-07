// Package synapse wraps the Synapse Admin REST API.
package synapse

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"time"
)

const (
	synapseBaseURL = "http://127.0.0.1:8008"
	adminPathV1    = "/_synapse/admin/v1"
	adminPathV2    = "/_synapse/admin/v2"
)

// Client talks to the Synapse Admin HTTP API using a stored admin token.
type Client struct {
	httpClient *http.Client
	dataDir    string
}

// NewClient creates a Client.
func NewClient(dataDir string) *Client {
	return &Client{
		httpClient: &http.Client{Timeout: 30 * time.Second},
		dataDir:    dataDir,
	}
}

// token retrieves the admin access token from disk (written by start.sh).
func (c *Client) token() (string, error) {
	tokenFile := filepath.Join(c.dataDir, "admin_token")
	raw, err := os.ReadFile(tokenFile)
	if err != nil {
		return "", fmt.Errorf("read admin token: %w (run setup to generate one)", err)
	}
	tok := string(bytes.TrimSpace(raw))
	if tok == "" {
		return "", fmt.Errorf("admin token file is empty")
	}
	return tok, nil
}

func (c *Client) do(method, path string, body interface{}) ([]byte, int, error) {
	tok, err := c.token()
	if err != nil {
		return nil, 0, err
	}

	var bodyReader io.Reader
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			return nil, 0, fmt.Errorf("marshal body: %w", err)
		}
		bodyReader = bytes.NewReader(b)
	}

	req, err := http.NewRequest(method, synapseBaseURL+path, bodyReader)
	if err != nil {
		return nil, 0, fmt.Errorf("create request: %w", err)
	}
	req.Header.Set("Authorization", "Bearer "+tok)
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return nil, 0, fmt.Errorf("http request: %w", err)
	}
	defer resp.Body.Close()

	data, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, resp.StatusCode, fmt.Errorf("read response: %w", err)
	}
	return data, resp.StatusCode, nil
}

func (c *Client) get(path string) ([]byte, error) {
	data, status, err := c.do("GET", path, nil)
	if err != nil {
		return nil, err
	}
	if status < 200 || status >= 300 {
		return nil, fmt.Errorf("synapse returned %d: %s", status, string(data))
	}
	return data, nil
}

func (c *Client) post(path string, body interface{}) ([]byte, error) {
	data, status, err := c.do("POST", path, body)
	if err != nil {
		return nil, err
	}
	if status < 200 || status >= 300 {
		return nil, fmt.Errorf("synapse returned %d: %s", status, string(data))
	}
	return data, nil
}

func (c *Client) put(path string, body interface{}) ([]byte, error) {
	data, status, err := c.do("PUT", path, body)
	if err != nil {
		return nil, err
	}
	if status < 200 || status >= 300 {
		return nil, fmt.Errorf("synapse returned %d: %s", status, string(data))
	}
	return data, nil
}

func (c *Client) delete(path string) ([]byte, error) {
	data, status, err := c.do("DELETE", path, nil)
	if err != nil {
		return nil, err
	}
	if status < 200 || status >= 300 {
		return nil, fmt.Errorf("synapse returned %d: %s", status, string(data))
	}
	return data, nil
}

// ---- Server info ----

// ServerVersion returns the Synapse server version.
func (c *Client) ServerVersion() (string, error) {
	data, err := c.get(adminPathV1 + "/server_version")
	if err != nil {
		return "", err
	}
	var resp struct {
		ServerVersion string `json:"server_version"`
	}
	if err := json.Unmarshal(data, &resp); err != nil {
		return "", err
	}
	return resp.ServerVersion, nil
}

// ---- Users ----

// User represents a Synapse user account.
type User struct {
	Name          string `json:"name"`
	DisplayName   string `json:"displayname"`
	AvatarURL     string `json:"avatar_url"`
	Admin         bool   `json:"admin"`
	Deactivated   bool   `json:"deactivated"`
	EraseData     bool   `json:"erase,omitempty"`
	PasswordHash  string `json:"password_hash,omitempty"`
	CreationTs    int64  `json:"creation_ts"`
	LastSeenTs    int64  `json:"last_seen_ts,omitempty"`
	GuestAccount  bool   `json:"is_guest"`
	ShadowBanned  bool   `json:"shadow_banned"`
	ConsentVersion string `json:"consent_version,omitempty"`
}

// UserList is the paginated list response.
type UserList struct {
	Users      []User `json:"users"`
	NextToken  string `json:"next_token"`
	Total      int    `json:"total"`
}

// ListUsers returns a page of users.
// Note: the guests parameter (both guests=true and guests=false) is not supported
// when Synapse delegates authentication to MAS. We never send it to ensure
// compatibility with both standalone Synapse and MAS-enabled Synapse.
func (c *Client) ListUsers(from int, limit int, guestsIncluded bool, searchTerm string) (*UserList, error) {
	params := url.Values{}
	params.Set("from", strconv.Itoa(from))
	params.Set("limit", strconv.Itoa(limit))
	// Do not send the guests parameter — unsupported when MAS is active.
	// With MAS, guest accounts are not used, so this is acceptable.
	if searchTerm != "" {
		params.Set("name", searchTerm)
	}

	data, err := c.get(adminPathV2 + "/users?" + params.Encode())
	if err != nil {
		return nil, err
	}
	var list UserList
	if err := json.Unmarshal(data, &list); err != nil {
		return nil, err
	}
	return &list, nil
}

// GetUser returns details for a single user.
func (c *Client) GetUser(userID string) (*User, error) {
	data, err := c.get(adminPathV2 + "/users/" + url.PathEscape(userID))
	if err != nil {
		return nil, err
	}
	var u User
	if err := json.Unmarshal(data, &u); err != nil {
		return nil, err
	}
	return &u, nil
}

// CreateOrUpdateUser creates or modifies a user account.
func (c *Client) CreateOrUpdateUser(userID string, body map[string]interface{}) error {
	_, err := c.put(adminPathV2+"/users/"+url.PathEscape(userID), body)
	return err
}

// ResetUserPassword sets a new password for the user.
func (c *Client) ResetUserPassword(userID, newPassword string, logoutAll bool) error {
	_, err := c.post(adminPathV1+"/reset_password/"+url.PathEscape(userID), map[string]interface{}{
		"new_password": newPassword,
		"logout_devices": logoutAll,
	})
	return err
}

// DeactivateUser deactivates (and optionally erases) a user.
func (c *Client) DeactivateUser(userID string, erase bool) error {
	_, err := c.post(adminPathV1+"/deactivate/"+url.PathEscape(userID), map[string]interface{}{
		"erase": erase,
	})
	return err
}

// PromoteUser sets or clears the admin flag for a user.
func (c *Client) PromoteUser(userID string, admin bool) error {
	_, err := c.put(adminPathV2+"/users/"+url.PathEscape(userID), map[string]interface{}{
		"admin": admin,
	})
	return err
}

// DeviceSession represents a single session for a device from the whois endpoint.
type DeviceSession struct {
	IPAddr    string `json:"ip"`
	UserAgent string `json:"user_agent"`
	LastSeen  int64  `json:"last_seen"`
}

// DeviceInfo holds device-level session info from the whois endpoint.
type DeviceInfo struct {
	DeviceID string          `json:"-"`
	Sessions []DeviceSession `json:"sessions"`
}

// ListUserDevices returns device and session info for a user via the /whois endpoint.
func (c *Client) ListUserDevices(userID string) ([]DeviceInfo, error) {
	data, err := c.get(adminPathV1 + "/whois/" + url.PathEscape(userID))
	if err != nil {
		return nil, err
	}

	// Response shape: {"user_id": "...", "devices": {"deviceId": {"sessions": [...]}}}
	var resp struct {
		Devices map[string]struct {
			Sessions []DeviceSession `json:"sessions"`
		} `json:"devices"`
	}
	if err := json.Unmarshal(data, &resp); err != nil {
		return nil, err
	}

	devices := make([]DeviceInfo, 0, len(resp.Devices))
	for id, d := range resp.Devices {
		devices = append(devices, DeviceInfo{DeviceID: id, Sessions: d.Sessions})
	}
	return devices, nil
}

// DeleteUserMedia deletes all media uploaded by a user.
func (c *Client) DeleteUserMedia(userID string) (int, error) {
	data, err := c.delete(adminPathV1 + "/users/" + url.PathEscape(userID) + "/media")
	if err != nil {
		return 0, err
	}
	var resp struct {
		Total int `json:"total"`
	}
	if err := json.Unmarshal(data, &resp); err != nil {
		return 0, err
	}
	return resp.Total, nil
}

// ---- Rooms ----

// Room represents a Matrix room.
type Room struct {
	RoomID        string `json:"room_id"`
	Name          string `json:"name"`
	CanonicalAlias string `json:"canonical_alias"`
	JoinedMembers int    `json:"joined_members"`
	JoinedLocalMembers int `json:"joined_local_members"`
	Version       string `json:"version"`
	Creator       string `json:"creator"`
	Encryption    string `json:"encryption"`
	Federatable   bool   `json:"federatable"`
	Public        bool   `json:"public"`
	JoinRules     string `json:"join_rules"`
	GuestAccess   string `json:"guest_access"`
	StateEvents   int    `json:"state_events"`
}

// RoomList is the paginated list response.
type RoomList struct {
	Rooms      []Room `json:"rooms"`
	NextBatch  string `json:"next_batch"`
	PrevBatch  string `json:"prev_batch"`
	TotalRooms int    `json:"total_rooms"`
}

// ListRooms returns a page of rooms.
func (c *Client) ListRooms(from int, limit int, searchTerm string) (*RoomList, error) {
	params := url.Values{}
	params.Set("from", strconv.Itoa(from))
	params.Set("limit", strconv.Itoa(limit))
	if searchTerm != "" {
		params.Set("search_term", searchTerm)
	}
	params.Set("order_by", "joined_members")
	params.Set("dir", "b")

	data, err := c.get(adminPathV1 + "/rooms?" + params.Encode())
	if err != nil {
		return nil, err
	}
	var list RoomList
	if err := json.Unmarshal(data, &list); err != nil {
		return nil, err
	}
	return &list, nil
}

// DeleteRoom deletes a room and all its content.
func (c *Client) DeleteRoom(roomID string, block bool, purge bool) (string, error) {
	// Synapse Admin API v2 expects purge/block in the JSON request body,
	// not as query parameters.
	body := map[string]interface{}{
		"purge": purge,
		"block": block,
	}
	data, status, err := c.do("DELETE", adminPathV2+"/rooms/"+url.PathEscape(roomID), body)
	if err != nil {
		return "", err
	}
	if status < 200 || status >= 300 {
		return "", fmt.Errorf("synapse returned %d: %s", status, string(data))
	}
	var resp struct {
		DeleteID string `json:"delete_id"`
	}
	if err := json.Unmarshal(data, &resp); err != nil {
		return "", err
	}
	return resp.DeleteID, nil
}

// MakeRoomAdmin makes a local user an admin of a room.
func (c *Client) MakeRoomAdmin(roomID, userID string) error {
	_, err := c.post(adminPathV1+"/rooms/"+url.PathEscape(roomID)+"/make_room_admin", map[string]interface{}{
		"user_id": userID,
	})
	return err
}

// ---- Server statistics ----

// Stats holds high-level server statistics.
type Stats struct {
	TotalUsers     int `json:"total_users"`
	TotalNonGuests int `json:"total_nonguest_users"`
	TotalRooms     int `json:"total_rooms"`
	TotalEvents    int `json:"total_events"`
	CacheSize      int `json:"cache_size"`
}

// GetStats fetches server statistics by querying the user list (for count) and room list.
// Note: Synapse has no single "server stats" endpoint; we derive counts from paginated list APIs.
func (c *Client) GetStats() (*Stats, error) {
	s := &Stats{}

	// Total users: omit guests parameter — not supported when MAS delegation is active.
	// This returns all users by default (including guests if any).
	usersData, err := c.get(adminPathV2 + "/users?limit=1")
	if err == nil {
		var resp struct {
			Total int `json:"total"`
		}
		if err := json.Unmarshal(usersData, &resp); err == nil {
			s.TotalUsers = resp.Total
			s.TotalNonGuests = resp.Total // approximate; guest distinction unavailable with MAS
		}
	}

	// Total rooms: same pattern
	roomsData, err := c.get(adminPathV1 + "/rooms?limit=1")
	if err == nil {
		var resp struct {
			TotalRooms int `json:"total_rooms"`
		}
		if err := json.Unmarshal(roomsData, &resp); err == nil {
			s.TotalRooms = resp.TotalRooms
		}
	}

	return s, nil
}

// ---- Registration tokens ----

// RegistrationToken is a one-time or limited-use token for registration.
type RegistrationToken struct {
	Token       string `json:"token"`
	UsesAllowed *int   `json:"uses_allowed"`
	Pending     int    `json:"pending"`
	Completed   int    `json:"completed"`
	ExpiryTime  *int64 `json:"expiry_time"`
}

// ListRegistrationTokens returns all registration tokens.
func (c *Client) ListRegistrationTokens() ([]RegistrationToken, error) {
	data, err := c.get(adminPathV1 + "/registration_tokens")
	if err != nil {
		return nil, err
	}
	var resp struct {
		RegistrationTokens []RegistrationToken `json:"registration_tokens"`
	}
	if err := json.Unmarshal(data, &resp); err != nil {
		return nil, err
	}
	return resp.RegistrationTokens, nil
}

// CreateRegistrationToken creates a new registration token.
func (c *Client) CreateRegistrationToken(token string, usesAllowed *int, expiryMs *int64) (*RegistrationToken, error) {
	body := map[string]interface{}{}
	if token != "" {
		body["token"] = token
	}
	if usesAllowed != nil {
		body["uses_allowed"] = *usesAllowed
	}
	if expiryMs != nil {
		body["expiry_time"] = *expiryMs
	}

	data, err := c.post(adminPathV1+"/registration_tokens/new", body)
	if err != nil {
		return nil, err
	}
	var tok RegistrationToken
	if err := json.Unmarshal(data, &tok); err != nil {
		return nil, err
	}
	return &tok, nil
}

// DeleteRegistrationToken removes a registration token.
func (c *Client) DeleteRegistrationToken(token string) error {
	_, err := c.delete(adminPathV1 + "/registration_tokens/" + url.PathEscape(token))
	return err
}

// ---- Background jobs / purge ----

// PurgeHistory purges old events from a room up to (but not including) beforeEventID.
// If deleteLocalEvents is true, locally-originated events are also deleted.
func (c *Client) PurgeHistory(roomID, beforeEventID string, deleteLocalEvents bool) (string, error) {
	body := map[string]interface{}{
		"delete_local_events": deleteLocalEvents,
	}
	if beforeEventID != "" {
		body["purge_up_to_event_id"] = beforeEventID
	}
	data, err := c.post(adminPathV1+"/purge_history/"+url.PathEscape(roomID), body)
	if err != nil {
		return "", err
	}
	var resp struct {
		PurgeID string `json:"purge_id"`
	}
	if err := json.Unmarshal(data, &resp); err != nil {
		return "", err
	}
	return resp.PurgeID, nil
}

// IsReady checks if Synapse is ready to serve requests.
func (c *Client) IsReady() bool {
	resp, err := c.httpClient.Get(synapseBaseURL + "/health")
	if err != nil {
		return false
	}
	resp.Body.Close()
	return resp.StatusCode == 200
}

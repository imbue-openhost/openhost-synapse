// Package config manages the openhost_settings.json file and homeserver.yaml patching.
package config

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
)

// Settings holds all admin-controlled configuration for this Synapse instance.
type Settings struct {
	// Registration
	OpenRegistration bool `json:"open_registration"`

	// Federation
	FederationEnabled bool `json:"federation_enabled"`

	// Rate limits
	RCLoginPerSecond  float64 `json:"rc_login_per_second"`
	RCLoginBurst      int     `json:"rc_login_burst"`

	// Retention / media
	MaxUploadSizeMB int `json:"max_upload_size_mb"`

	// Password policy
	PasswordMinLength int  `json:"password_min_length"`
	PasswordRequireDigit bool `json:"password_require_digit"`
	PasswordRequireSymbol bool `json:"password_require_symbol"`

	// Room defaults
	AllowPublicRooms bool `json:"allow_public_rooms"`
}

var defaults = Settings{
	OpenRegistration:    true,
	FederationEnabled:   false,
	RCLoginPerSecond:    10,
	RCLoginBurst:        50,
	MaxUploadSizeMB:     50,
	PasswordMinLength:   8,
	PasswordRequireDigit: false,
	PasswordRequireSymbol: false,
	AllowPublicRooms:    true,
}

// Config is the top-level configuration manager.
type Config struct {
	mu            sync.RWMutex
	dataDir       string
	settingsFile  string
	homeserverYAML string
}

// New creates a Config for the given data directory.
func New(dataDir string) *Config {
	return &Config{
		dataDir:        dataDir,
		settingsFile:   filepath.Join(dataDir, "openhost_settings.json"),
		homeserverYAML: filepath.Join(dataDir, "homeserver.yaml"),
	}
}

// DataDir returns the data directory.
func (c *Config) DataDir() string { return c.dataDir }

// HomeserverYAML returns the path to homeserver.yaml.
func (c *Config) HomeserverYAML() string { return c.homeserverYAML }

// Load reads settings from disk, filling defaults for missing keys.
func (c *Config) Load() (*Settings, error) {
	c.mu.RLock()
	defer c.mu.RUnlock()

	data, err := os.ReadFile(c.settingsFile)
	if err != nil {
		if os.IsNotExist(err) {
			s := defaults
			return &s, nil
		}
		return nil, fmt.Errorf("read settings: %w", err)
	}

	s := defaults // start with defaults
	if err := json.Unmarshal(data, &s); err != nil {
		return nil, fmt.Errorf("parse settings: %w", err)
	}
	return &s, nil
}

// Save persists settings to disk and patches homeserver.yaml.
func (c *Config) Save(s *Settings) error {
	c.mu.Lock()
	defer c.mu.Unlock()

	data, err := json.MarshalIndent(s, "", "  ")
	if err != nil {
		return fmt.Errorf("marshal settings: %w", err)
	}
	data = append(data, '\n')

	if err := os.WriteFile(c.settingsFile, data, 0o644); err != nil {
		return fmt.Errorf("write settings: %w", err)
	}

	if err := c.patchYAML(s); err != nil {
		return fmt.Errorf("patch yaml: %w", err)
	}
	return nil
}

// patchYAML applies settings to homeserver.yaml. Must be called with c.mu held.
func (c *Config) patchYAML(s *Settings) error {
	raw, err := os.ReadFile(c.homeserverYAML)
	if err != nil {
		if os.IsNotExist(err) {
			return nil // not yet generated; settings will be applied on first boot
		}
		return fmt.Errorf("read homeserver.yaml: %w", err)
	}
	content := string(raw)

	// Registration
	content = setYAMLBool(content, "enable_registration", s.OpenRegistration)
	content = setYAMLBool(content, "enable_registration_without_verification", s.OpenRegistration)

	// Federation
	content = patchFederationListener(content, s.FederationEnabled)
	content = patchFederationDomainWhitelist(content, s.FederationEnabled)

	// Public rooms
	content = setYAMLBool(content, "allow_public_rooms_without_auth", s.AllowPublicRooms)
	content = setYAMLBool(content, "allow_public_rooms_over_federation", s.FederationEnabled && s.AllowPublicRooms)

	// Upload size
	content = setYAMLValue(content, "max_upload_size", fmt.Sprintf("%dM", s.MaxUploadSizeMB))

	// Password policy (Synapse uses a nested block)
	content = patchPasswordPolicy(content, s)

	// Rate limits — write/update the rc_login block
	content = patchRCLogin(content, s)

	if err := os.WriteFile(c.homeserverYAML, []byte(content), 0o644); err != nil {
		return fmt.Errorf("write homeserver.yaml: %w", err)
	}
	return nil
}

// ---- YAML manipulation helpers ----

func setYAMLBool(content, key string, value bool) string {
	val := "false"
	if value {
		val = "true"
	}
	return setYAMLValue(content, key, val)
}

func setYAMLValue(content, key, value string) string {
	pattern := regexp.MustCompile(`(?m)^` + regexp.QuoteMeta(key) + `:.*$`)
	replacement := key + ": " + value
	if pattern.MatchString(content) {
		return pattern.ReplaceAllString(content, replacement)
	}
	return strings.TrimRight(content, "\n") + "\n" + replacement + "\n"
}

func patchFederationListener(content string, enabled bool) string {
	re := regexp.MustCompile(`((?:-\s+)?names:\s*\[)client(?:,\s*federation)?\]`)
	if !re.MatchString(content) {
		return content
	}
	return re.ReplaceAllStringFunc(content, func(match string) string {
		if enabled {
			return re.ReplaceAllString(match, "${1}client, federation]")
		}
		return re.ReplaceAllString(match, "${1}client]")
	})
}

func patchFederationDomainWhitelist(content string, enabled bool) string {
	// Remove existing whitelist and its comment
	content = regexp.MustCompile(`\n# Federation disabled[^\n]*\n`).ReplaceAllString(content, "\n")
	content = regexp.MustCompile(`(?m)^federation_domain_whitelist:.*$`).ReplaceAllString(content, "")
	content = regexp.MustCompile(`\n{3,}`).ReplaceAllString(content, "\n\n")

	if !enabled {
		content = strings.TrimRight(content, "\n") + "\n\n# Federation disabled — personal server.\nfederation_domain_whitelist: []\n"
	}
	return content
}

func patchPasswordPolicy(content string, s *Settings) string {
	// Remove existing password_config block including all indented sub-keys.
	// The (?ms) flag makes . match newlines and ^ match line-starts.
	content = regexp.MustCompile(`(?ms)^password_config:\n(?:[ \t]+.*\n)*`).ReplaceAllString(content, "")
	content = regexp.MustCompile(`\n{3,}`).ReplaceAllString(content, "\n\n")

	block := fmt.Sprintf("password_config:\n  minimum_length: %d\n  require_digit: %v\n  require_punctuation: %v\n",
		s.PasswordMinLength, s.PasswordRequireDigit, s.PasswordRequireSymbol)
	content = strings.TrimRight(content, "\n") + "\n" + block
	return content
}

func patchRCLogin(content string, s *Settings) string {
	// Remove existing rc_login block
	re := regexp.MustCompile(`(?ms)^rc_login:\n(?:[ \t]+.*\n)*`)
	content = re.ReplaceAllString(content, "")
	content = regexp.MustCompile(`\n{3,}`).ReplaceAllString(content, "\n\n")

	block := fmt.Sprintf(`rc_login:
  address:
    per_second: %.1f
    burst_count: %d
  account:
    per_second: %.1f
    burst_count: %d
  failed_attempts:
    per_second: %.1f
    burst_count: %d
`, s.RCLoginPerSecond, s.RCLoginBurst, s.RCLoginPerSecond, s.RCLoginBurst, s.RCLoginPerSecond, s.RCLoginBurst)
	content = strings.TrimRight(content, "\n") + "\n" + block
	return content
}

package config

import (
	"strings"
	"testing"
)

// sampleYAML is a minimal homeserver.yaml fixture that exercises the patcher.
const sampleYAML = `server_name: "example.com"
pid_file: /data/homeserver.pid
listeners:
  - port: 8448
    tls: false
    type: http
    x_forwarded: true
    bind_addresses: ['::1', '127.0.0.1']
    resources:
      - names: [client]
        compress: false

log_config: "/data/example.com.log.config"
media_store_path: /data/media_store
enable_registration: false
enable_registration_without_verification: false
`

func TestSetYAMLBool_Update(t *testing.T) {
	got := setYAMLBool(sampleYAML, "enable_registration", true)
	if !strings.Contains(got, "enable_registration: true") {
		t.Errorf("expected 'enable_registration: true' in output, got:\n%s", got)
	}
	// The original had enable_registration: false; after update it must be true exactly once.
	if strings.Count(got, "enable_registration: true") != 1 {
		t.Errorf("expected exactly one 'enable_registration: true', got:\n%s", got)
	}
	if strings.Contains(got, "enable_registration: false") {
		t.Errorf("old value still present, got:\n%s", got)
	}
}

func TestSetYAMLBool_Append(t *testing.T) {
	content := "server_name: test\n"
	got := setYAMLBool(content, "new_key", true)
	if !strings.Contains(got, "new_key: true") {
		t.Errorf("expected key to be appended, got:\n%s", got)
	}
}

func TestPatchFederationListener_Enable(t *testing.T) {
	got := patchFederationListener(sampleYAML, true)
	if !strings.Contains(got, "[client, federation]") {
		t.Errorf("expected federation listener enabled, got:\n%s", got)
	}
}

func TestPatchFederationListener_Disable(t *testing.T) {
	content := strings.Replace(sampleYAML, "[client]", "[client, federation]", 1)
	got := patchFederationListener(content, false)
	if strings.Contains(got, "federation]") {
		t.Errorf("expected federation listener removed, got:\n%s", got)
	}
	if !strings.Contains(got, "[client]") {
		t.Errorf("expected [client] present, got:\n%s", got)
	}
}

func TestPatchFederationDomainWhitelist_Disable(t *testing.T) {
	got := patchFederationDomainWhitelist(sampleYAML, false)
	if !strings.Contains(got, "federation_domain_whitelist: []") {
		t.Errorf("expected whitelist disabled, got:\n%s", got)
	}
}

func TestPatchFederationDomainWhitelist_Enable(t *testing.T) {
	content := sampleYAML + "\nfederation_domain_whitelist: []\n"
	got := patchFederationDomainWhitelist(content, true)
	if strings.Contains(got, "federation_domain_whitelist:") {
		t.Errorf("expected whitelist removed when enabled, got:\n%s", got)
	}
}

func TestPatchPasswordPolicy_Idempotent(t *testing.T) {
	s := &Settings{PasswordMinLength: 12, PasswordRequireDigit: true, PasswordRequireSymbol: false}
	once := patchPasswordPolicy(sampleYAML, s)
	twice := patchPasswordPolicy(once, s)

	// Should not accumulate orphaned lines
	count1 := strings.Count(once, "password_config:")
	count2 := strings.Count(twice, "password_config:")
	if count1 != 1 {
		t.Errorf("first apply: expected 1 password_config block, got %d", count1)
	}
	if count2 != 1 {
		t.Errorf("second apply: expected 1 password_config block, got %d (orphaned lines accumulating)", count2)
	}

	if !strings.Contains(twice, "minimum_length: 12") {
		t.Errorf("expected minimum_length: 12, got:\n%s", twice)
	}
}

func TestPatchRCLogin_Idempotent(t *testing.T) {
	s := &Settings{RCLoginPerSecond: 5, RCLoginBurst: 20}
	once := patchRCLogin(sampleYAML, s)
	twice := patchRCLogin(once, s)

	count1 := strings.Count(once, "rc_login:")
	count2 := strings.Count(twice, "rc_login:")
	if count1 != 1 {
		t.Errorf("first apply: expected 1 rc_login block, got %d", count1)
	}
	if count2 != 1 {
		t.Errorf("second apply: expected 1 rc_login block, got %d", count2)
	}
}

func TestPatchPasswordPolicy_SubkeyNotOrphaned(t *testing.T) {
	s1 := &Settings{PasswordMinLength: 8, PasswordRequireDigit: false}
	s2 := &Settings{PasswordMinLength: 16, PasswordRequireDigit: true}

	content := patchPasswordPolicy(sampleYAML, s1)
	content = patchPasswordPolicy(content, s2)

	// Old sub-keys should not appear
	if strings.Contains(content, "minimum_length: 8") {
		t.Errorf("old sub-key minimum_length: 8 still present after update:\n%s", content)
	}
	if !strings.Contains(content, "minimum_length: 16") {
		t.Errorf("new sub-key minimum_length: 16 not found:\n%s", content)
	}
	if !strings.Contains(content, "require_digit: true") {
		t.Errorf("require_digit: true not found:\n%s", content)
	}
}

package config

import (
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"
)

func TestLoadAppliesDefaultsAndReadsTokenFile(t *testing.T) {
	t.Setenv("COMMA_COMPANION_TOKEN", "")
	t.Setenv("COMMA_COMPANION_TOKEN_FILE", "")
	dir := t.TempDir()
	root := filepath.Join(dir, "realdata")
	spool := filepath.Join(dir, "spool")
	token := filepath.Join(dir, "token")
	if err := os.WriteFile(token, []byte("0123456789abcdef0123456789abcdef\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(dir, "config.json")
	raw := `{
  "server_url": "https://example.invalid/",
  "device_id": "device",
  "token_file": ` + quote(token) + `,
  "roots": [{"name": "realdata", "path": ` + quote(root) + `}],
  "spool_dir": ` + quote(spool) + `
}`
	if err := os.WriteFile(path, []byte(raw), 0o600); err != nil {
		t.Fatal(err)
	}
	cfg, err := Load(path)
	if err != nil {
		t.Fatal(err)
	}
	if cfg.Token != "0123456789abcdef0123456789abcdef" || cfg.ServerURL != "https://example.invalid" {
		t.Fatalf("unexpected loaded config: %#v", cfg)
	}
	if cfg.ChunkSize != DefaultChunkSize || cfg.Storage.EmergencyBehavior != "pause" {
		t.Fatal("defaults were not retained")
	}
}

func TestRejectsSpoolInsideLoggingRoot(t *testing.T) {
	dir := t.TempDir()
	cfg := Defaults()
	cfg.ServerURL = "https://example.invalid"
	cfg.DeviceID = "device"
	cfg.TokenFile = filepath.Join(dir, "token")
	cfg.Roots = []Root{{Name: "realdata", Path: filepath.Join(dir, "realdata")}}
	cfg.SpoolDir = filepath.Join(dir, "realdata", "spool")
	if err := cfg.Validate(); err == nil {
		t.Fatal("spool inside logging root was accepted")
	}
}

func TestRejectsJournalOutsideSpool(t *testing.T) {
	dir := t.TempDir()
	cfg := Defaults()
	cfg.ServerURL = "https://example.invalid"
	cfg.DeviceID = "device"
	cfg.TokenFile = filepath.Join(dir, "token")
	cfg.Roots = []Root{{Name: "realdata", Path: filepath.Join(dir, "realdata")}}
	cfg.SpoolDir = filepath.Join(dir, "spool")
	cfg.JournalPath = filepath.Join(dir, "state", "journal.json")
	if err := cfg.Validate(); err == nil {
		t.Fatal("journal outside spool was accepted")
	}
}

func TestLoadRejectsUnknownAndInlineTokenFields(t *testing.T) {
	t.Setenv("COMMA_COMPANION_TOKEN", "")
	t.Setenv("COMMA_COMPANION_TOKEN_FILE", "")
	dir := t.TempDir()
	path := filepath.Join(dir, "config.json")
	raw := `{
  "server_url": "https://example.invalid",
  "device_id": "device",
  "token": "must-not-be-inline",
  "token_file": ` + quote(filepath.Join(dir, "token")) + `,
  "roots": [{"name": "realdata", "path": ` + quote(filepath.Join(dir, "realdata")) + `}],
  "spool_dir": ` + quote(filepath.Join(dir, "spool")) + `
}`
	if err := os.WriteFile(path, []byte(raw), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := Load(path); err == nil {
		t.Fatal("inline token field was accepted")
	}
}

func TestLoadRejectsSecretTokenEnvironment(t *testing.T) {
	t.Setenv("COMMA_COMPANION_TOKEN", "do-not-use-secret-env")
	t.Setenv("COMMA_COMPANION_TOKEN_FILE", "")
	dir := t.TempDir()
	path := filepath.Join(dir, "config.json")
	raw := `{
  "server_url": "https://example.invalid",
  "device_id": "device",
  "token_file": ` + quote(filepath.Join(dir, "token")) + `,
  "roots": [{"name": "realdata", "path": ` + quote(filepath.Join(dir, "realdata")) + `}],
  "spool_dir": ` + quote(filepath.Join(dir, "spool")) + `
}`
	if err := os.WriteFile(path, []byte(raw), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := Load(path); err == nil {
		t.Fatal("secret-bearing token environment override was accepted")
	}
}

func TestTokenFilePathEnvironmentOverrideIsNonSecret(t *testing.T) {
	t.Setenv("COMMA_COMPANION_TOKEN", "")
	dir := t.TempDir()
	configuredToken := filepath.Join(dir, "configured-token")
	overrideToken := filepath.Join(dir, "credential-token")
	if err := os.WriteFile(overrideToken, []byte("abcdef0123456789abcdef0123456789"), 0o600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("COMMA_COMPANION_TOKEN_FILE", overrideToken)
	path := filepath.Join(dir, "config.json")
	raw := `{
  "server_url": "https://example.invalid",
  "device_id": "device",
  "token_file": ` + quote(configuredToken) + `,
  "roots": [{"name": "realdata", "path": ` + quote(filepath.Join(dir, "realdata")) + `}],
  "spool_dir": ` + quote(filepath.Join(dir, "spool")) + `
}`
	if err := os.WriteFile(path, []byte(raw), 0o600); err != nil {
		t.Fatal(err)
	}
	cfg, err := Load(path)
	if err != nil {
		t.Fatal(err)
	}
	if cfg.TokenFile != overrideToken || cfg.Token != "abcdef0123456789abcdef0123456789" {
		t.Fatalf("token path override was not applied safely: %#v", cfg)
	}
}

func TestDisruptiveCommandDefaultsAreDisabled(t *testing.T) {
	cfg := Defaults()
	if cfg.Commands.AllowAgentRestart || cfg.Commands.AllowStarPilotRestart ||
		cfg.Commands.AllowPowerCommands {
		t.Fatalf("disruptive commands defaulted on: %#v", cfg.Commands)
	}
}

func TestUploadDefaultsAllowOnroadUnmeteredCellular(t *testing.T) {
	cfg := Defaults()
	if cfg.Policy.UploadOnlyOffroad || cfg.Policy.RequireWiFi || cfg.Policy.AllowMetered {
		t.Fatalf("unexpected upload policy defaults: %#v", cfg.Policy)
	}
}

func TestOffroadStateAgeLimitDefaultsDisabledAndRejectsNegative(t *testing.T) {
	dir := t.TempDir()
	cfg := Defaults()
	cfg.ServerURL = "https://example.invalid"
	cfg.DeviceID = "device"
	cfg.TokenFile = filepath.Join(dir, "token")
	cfg.Roots = []Root{{Name: "realdata", Path: filepath.Join(dir, "realdata")}}
	cfg.SpoolDir = filepath.Join(dir, "spool")
	if cfg.Policy.OffroadMaxAge.Duration != 0 {
		t.Fatalf("offroad state age limit defaulted on: %s", cfg.Policy.OffroadMaxAge)
	}
	if err := cfg.Validate(); err != nil {
		t.Fatalf("disabled offroad state age limit was rejected: %v", err)
	}
	cfg.Policy.OffroadMaxAge = Duration{Duration: -time.Second}
	if err := cfg.Validate(); err == nil ||
		!strings.Contains(err.Error(), "non-negative") {
		t.Fatalf("negative offroad state age limit was accepted: %v", err)
	}
}

func TestPrivilegedCommandsRequireSeparateSocketAndCredential(t *testing.T) {
	dir := t.TempDir()
	cfg := Defaults()
	cfg.ServerURL = "https://example.invalid"
	cfg.DeviceID = "device"
	cfg.TokenFile = filepath.Join(dir, "device-token")
	cfg.Roots = []Root{{Name: "realdata", Path: filepath.Join(dir, "realdata")}}
	cfg.SpoolDir = filepath.Join(dir, "spool")
	cfg.Commands.AllowStarPilotRestart = true
	if err := cfg.Validate(); err == nil ||
		!strings.Contains(err.Error(), "control_socket") {
		t.Fatalf("privileged command without socket was accepted: %v", err)
	}
	cfg.Commands.ControlSocket = filepath.Join(dir, "control.sock")
	if err := cfg.Validate(); err == nil ||
		!strings.Contains(err.Error(), "CONTROL_TOKEN_FILE") {
		t.Fatalf("privileged command without separate credential was accepted: %v", err)
	}
	cfg.ControlTokenFile = filepath.Join(dir, "control-token")
	if err := cfg.Validate(); err != nil {
		t.Fatalf("fully specified staged privileged config was rejected: %v", err)
	}
}

func TestLoadRejectsPlaintextControlTokenEnvironment(t *testing.T) {
	t.Setenv("COMMA_COMPANION_TOKEN", "")
	t.Setenv("COMMA_COMPANION_TOKEN_FILE", "")
	t.Setenv("COMMA_COMPANION_CONTROL_TOKEN", "must-not-be-in-environment")
	dir := t.TempDir()
	path := filepath.Join(dir, "config.json")
	raw := `{
  "server_url": "https://example.invalid",
  "device_id": "device",
  "token_file": ` + quote(filepath.Join(dir, "device-token")) + `,
  "roots": [{"name": "realdata", "path": ` + quote(filepath.Join(dir, "realdata")) + `}],
  "spool_dir": ` + quote(filepath.Join(dir, "spool")) + `
}`
	if err := os.WriteFile(path, []byte(raw), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := Load(path); err == nil ||
		!strings.Contains(err.Error(), "CONTROL_TOKEN") {
		t.Fatalf("plaintext control credential environment was accepted: %v", err)
	}
}

func TestReadTokenRejectsSymlink(t *testing.T) {
	dir := t.TempDir()
	target := filepath.Join(dir, "target")
	if err := os.WriteFile(target, []byte("abcdef0123456789abcdef0123456789"), 0o600); err != nil {
		t.Fatal(err)
	}
	link := filepath.Join(dir, "token")
	if err := os.Symlink(target, link); err != nil {
		t.Skipf("symlink unavailable: %v", err)
	}
	if _, err := readTokenFile(link); err == nil {
		t.Fatal("symlink token file was accepted")
	}
}

func TestReadTokenRejectsPermissiveLinuxMode(t *testing.T) {
	if runtime.GOOS != "linux" {
		t.Skip("Linux permission contract")
	}
	path := filepath.Join(t.TempDir(), "token")
	if err := os.WriteFile(path, []byte("abcdef0123456789abcdef0123456789"), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := readTokenFile(path); err == nil {
		t.Fatal("group/world-readable token file was accepted")
	}
}

func TestValidateDevicePathsUsesDirectRealdataRoots(t *testing.T) {
	if runtime.GOOS != "linux" {
		t.Skip("Linux device path contract")
	}
	cfg := Defaults()
	cfg.Roots = []Root{{Name: "realdata", Path: "/data/media/0/realdata"}}
	if err := cfg.ValidateDevicePaths(); err != nil {
		t.Fatalf("approved device root rejected: %v", err)
	}
	cfg.Roots[0].Path = "/data/media/0/realdata-alt"
	if err := cfg.ValidateDevicePaths(); err != nil {
		t.Fatalf("approved alternate realdata root rejected: %v", err)
	}
	cfg.Roots[0].Path = "/tmp/realdata"
	if err := cfg.ValidateDevicePaths(); err == nil {
		t.Fatal("non-device root was accepted")
	}
	cfg.Roots[0].Path = "/data/media/0/nested/realdata"
	if err := cfg.ValidateDevicePaths(); err == nil {
		t.Fatal("nested device root was accepted")
	}
}

func TestLoadForInspectionDoesNotRequireOrReadToken(t *testing.T) {
	t.Setenv("COMMA_COMPANION_TOKEN", "inspection-must-ignore-secret-environment")
	t.Setenv("COMMA_COMPANION_TOKEN_FILE", filepath.Join(t.TempDir(), "missing-override"))
	dir := t.TempDir()
	path := filepath.Join(dir, "config.json")
	raw := `{
  "server_url": "https://example.invalid",
  "device_id": "device",
  "roots": [{"name": "realdata", "path": ` + quote(filepath.Join(dir, "realdata")) + `}],
  "spool_dir": ` + quote(filepath.Join(dir, "spool")) + `,
  "inventory": {"expected_streams": []}
}`
	if err := os.WriteFile(path, []byte(raw), 0o600); err != nil {
		t.Fatal(err)
	}
	cfg, err := LoadForInspection(path)
	if err != nil {
		t.Fatal(err)
	}
	if cfg.Token != "" || cfg.TokenFile != "" {
		t.Fatalf("inspection unexpectedly loaded a credential: %#v", cfg)
	}
}

func TestInventoryExpectedStreamsAreCanonicalAndRequireRLogPerProfile(t *testing.T) {
	dir := t.TempDir()
	base := Defaults()
	base.ServerURL = "https://example.invalid"
	base.DeviceID = "device"
	base.TokenFile = filepath.Join(dir, "token")
	base.Roots = []Root{
		{Name: "realdata", Path: filepath.Join(dir, "realdata")},
		{Name: "realdata_HD", Path: filepath.Join(dir, "realdata_HD")},
	}
	base.SpoolDir = filepath.Join(dir, "spool")
	base.JournalPath = filepath.Join(base.SpoolDir, "journal.json")

	tests := []struct {
		name    string
		streams []InventoryStream
		ok      bool
	}{
		{
			name: "root profiles include rlog",
			streams: []InventoryStream{
				{RootName: "realdata", ArtifactType: " RLOG "},
				{RootName: "realdata", ArtifactType: "VIDEO", Camera: "Road"},
				{RootName: "realdata_HD", ArtifactType: "rlog"},
				{RootName: "realdata_HD", ArtifactType: "video", Camera: "wide"},
			},
			ok: true,
		},
		{
			name: "profile lacks rlog",
			streams: []InventoryStream{
				{RootName: "realdata", ArtifactType: "rlog"},
				{RootName: "realdata_HD", ArtifactType: "video", Camera: "wide"},
			},
		},
		{
			name: "duplicate canonical role",
			streams: []InventoryStream{
				{RootName: "realdata", ArtifactType: "rlog"},
				{RootName: "realdata", ArtifactType: " RLOG "},
			},
		},
		{
			name: "unknown root",
			streams: []InventoryStream{
				{RootName: "other", ArtifactType: "rlog"},
			},
		},
		{
			name: "log camera",
			streams: []InventoryStream{
				{RootName: "realdata", ArtifactType: "rlog", Camera: "road"},
			},
		},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			cfg := base
			cfg.Roots = append([]Root(nil), base.Roots...)
			cfg.Inventory.ExpectedStreams = append([]InventoryStream(nil), test.streams...)
			err := cfg.Validate()
			if test.ok && err != nil {
				t.Fatalf("valid inventory config rejected: %v", err)
			}
			if !test.ok && err == nil {
				t.Fatal("invalid inventory config accepted")
			}
			if test.ok {
				if cfg.Inventory.ExpectedStreams[0].ArtifactType != "rlog" ||
					cfg.Inventory.ExpectedStreams[1].Camera != "road" {
					t.Fatalf("inventory config was not normalized: %#v", cfg.Inventory)
				}
			}
		})
	}
}

func quote(value string) string {
	result := `"`
	for _, character := range value {
		switch character {
		case '\\':
			result += `\\`
		case '"':
			result += `\"`
		default:
			result += string(character)
		}
	}
	return result + `"`
}

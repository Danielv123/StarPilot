package config

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/url"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"time"

	"starpilot.local/comma-companion-agent/internal/inventory"
)

const (
	DefaultChunkSize = 512 * 1024
)

type Duration struct {
	time.Duration
}

func (d *Duration) UnmarshalJSON(data []byte) error {
	var value string
	if err := json.Unmarshal(data, &value); err != nil {
		return fmt.Errorf("duration must be a string: %w", err)
	}
	parsed, err := time.ParseDuration(value)
	if err != nil {
		return err
	}
	d.Duration = parsed
	return nil
}

func (d Duration) MarshalJSON() ([]byte, error) {
	return json.Marshal(d.String())
}

type Root struct {
	Name string `json:"name"`
	Path string `json:"path"`
}

type Policy struct {
	UploadOnlyOffroad     bool     `json:"upload_only_offroad"`
	RequireWiFi           bool     `json:"require_wifi"`
	WiFiInterface         string   `json:"wifi_interface"`
	NetworkTypeFile       string   `json:"network_type_file"`
	MeteredStateFile      string   `json:"metered_state_file"`
	OffroadStateFile      string   `json:"offroad_state_file"`
	OnroadStateFile       string   `json:"onroad_state_file"`
	OffroadStableDuration Duration `json:"offroad_stable_duration"`
	OffroadMaxAge         Duration `json:"offroad_max_age"`
	AllowMetered          bool     `json:"allow_metered"`
	TrueValues            []string `json:"true_values"`
}

type Commands struct {
	AllowAgentRestart     bool   `json:"allow_agent_restart"`
	AllowStarPilotRestart bool   `json:"allow_starpilot_restart"`
	AllowPowerCommands    bool   `json:"allow_power_commands"`
	ControlSocket         string `json:"control_socket,omitempty"`
}

type Storage struct {
	MaxRetainedBytes  int64  `json:"max_retained_bytes"`
	MinFreeBytes      int64  `json:"min_free_bytes"`
	EmergencyBehavior string `json:"emergency_behavior"`
}

type InventoryStream struct {
	RootName     string `json:"root_name"`
	ArtifactType string `json:"artifact_type"`
	Camera       string `json:"camera,omitempty"`
}

type Inventory struct {
	ExpectedStreams []InventoryStream `json:"expected_streams"`
}

type Config struct {
	ServerURL            string    `json:"server_url"`
	AllowInsecureHTTP    bool      `json:"allow_insecure_http"`
	DeviceID             string    `json:"device_id"`
	Token                string    `json:"-"`
	TokenFile            string    `json:"token_file,omitempty"`
	ControlToken         string    `json:"-"`
	ControlTokenFile     string    `json:"-"`
	ReadyFile            string    `json:"-"`
	Roots                []Root    `json:"roots"`
	SpoolDir             string    `json:"spool_dir"`
	JournalPath          string    `json:"journal_path"`
	ScanInterval         Duration  `json:"scan_interval"`
	UploadPollInterval   Duration  `json:"upload_poll_interval"`
	HeartbeatInterval    Duration  `json:"heartbeat_interval"`
	LongPollTimeout      Duration  `json:"long_poll_timeout"`
	HTTPTimeout          Duration  `json:"http_timeout"`
	StableObservations   int       `json:"stable_observations"`
	StableDuration       Duration  `json:"stable_duration"`
	FinalSegmentGrace    Duration  `json:"final_segment_grace"`
	OffroadSegmentGrace  Duration  `json:"offroad_segment_grace"`
	NonSegmentGrace      Duration  `json:"non_segment_grace"`
	ChunkSize            int64     `json:"chunk_size"`
	MaxFilesPerScan      int       `json:"max_files_per_scan"`
	MaxConcurrentUploads int       `json:"max_concurrent_uploads"`
	UserAgent            string    `json:"user_agent"`
	Policy               Policy    `json:"policy"`
	Storage              Storage   `json:"storage"`
	Commands             Commands  `json:"commands"`
	Inventory            Inventory `json:"inventory"`
}

func Defaults() Config {
	return Config{
		ScanInterval:         Duration{30 * time.Second},
		UploadPollInterval:   Duration{2 * time.Second},
		HeartbeatInterval:    Duration{5 * time.Second},
		LongPollTimeout:      Duration{25 * time.Second},
		HTTPTimeout:          Duration{45 * time.Second},
		StableObservations:   2,
		StableDuration:       Duration{45 * time.Second},
		FinalSegmentGrace:    Duration{10 * time.Minute},
		OffroadSegmentGrace:  Duration{90 * time.Second},
		NonSegmentGrace:      Duration{5 * time.Minute},
		ChunkSize:            DefaultChunkSize,
		MaxFilesPerScan:      8,
		MaxConcurrentUploads: 1,
		UserAgent:            "comma-companion-agent/dev",
		Policy: Policy{
			UploadOnlyOffroad:     false,
			RequireWiFi:           false,
			WiFiInterface:         "wlan0",
			OffroadStateFile:      "/data/params/d/IsOffroad",
			OnroadStateFile:       "/data/params/d/IsOnroad",
			OffroadStableDuration: Duration{10 * time.Second},
			OffroadMaxAge:         Duration{0},
			MeteredStateFile:      "/data/params/d/NetworkMetered",
			TrueValues:            []string{"1", "true", "yes", "on"},
		},
		Storage: Storage{
			MaxRetainedBytes:  12 * 1024 * 1024 * 1024,
			MinFreeBytes:      8 * 1024 * 1024 * 1024,
			EmergencyBehavior: "pause",
		},
		Commands: Commands{
			AllowAgentRestart:     false,
			AllowStarPilotRestart: false,
			AllowPowerCommands:    false,
		},
	}
}

func Load(path string) (Config, error) {
	cfg, err := decode(path)
	if err != nil {
		return Config{}, err
	}
	if os.Getenv("COMMA_COMPANION_TOKEN") != "" {
		return Config{}, errors.New("COMMA_COMPANION_TOKEN is forbidden; use a protected token file")
	}
	if os.Getenv("COMMA_COMPANION_CONTROL_TOKEN") != "" {
		return Config{}, errors.New(
			"COMMA_COMPANION_CONTROL_TOKEN is forbidden; use a protected control credential file",
		)
	}
	if override := strings.TrimSpace(os.Getenv("COMMA_COMPANION_TOKEN_FILE")); override != "" {
		cfg.TokenFile = override
	}
	cfg.ReadyFile = strings.TrimSpace(os.Getenv("COMMA_COMPANION_READY_FILE"))
	cfg.ControlTokenFile = strings.TrimSpace(
		os.Getenv("COMMA_COMPANION_CONTROL_TOKEN_FILE"),
	)
	if err := cfg.validate(true); err != nil {
		return Config{}, err
	}
	token, err := readTokenFile(cfg.TokenFile)
	if err != nil {
		return Config{}, err
	}
	cfg.Token = token
	if cfg.Commands.AllowStarPilotRestart || cfg.Commands.AllowPowerCommands {
		controlToken, err := readTokenFile(cfg.ControlTokenFile)
		if err != nil {
			return Config{}, fmt.Errorf("read control credential: %w", err)
		}
		cfg.ControlToken = controlToken
	}
	return cfg, nil
}

func LoadForInspection(path string) (Config, error) {
	cfg, err := decode(path)
	if err != nil {
		return Config{}, err
	}
	if err := cfg.validate(false); err != nil {
		return Config{}, err
	}
	return cfg, nil
}

func decode(path string) (Config, error) {
	cfg := Defaults()
	data, err := os.ReadFile(path)
	if err != nil {
		return Config{}, fmt.Errorf("read config: %w", err)
	}
	if len(data) > 1<<20 {
		return Config{}, errors.New("config exceeds 1 MiB")
	}
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&cfg); err != nil {
		return Config{}, fmt.Errorf("parse config: %w", err)
	}
	if err := ensureJSONEOF(decoder); err != nil {
		return Config{}, fmt.Errorf("parse config: %w", err)
	}
	return cfg, nil
}

func (c *Config) Validate() error {
	return c.validate(true)
}

func (c *Config) validate(requireToken bool) error {
	c.ServerURL = strings.TrimRight(strings.TrimSpace(c.ServerURL), "/")
	if c.ServerURL == "" {
		return errors.New("server_url is required")
	}
	parsedURL, err := url.Parse(c.ServerURL)
	if err != nil || parsedURL.Host == "" || parsedURL.User != nil ||
		(parsedURL.Path != "" && parsedURL.Path != "/") || parsedURL.RawQuery != "" || parsedURL.Fragment != "" {
		return errors.New("server_url must be an origin without credentials, path, query, or fragment")
	}
	if parsedURL.Scheme != "https" {
		if !c.AllowInsecureHTTP || parsedURL.Scheme != "http" {
			return errors.New("server_url must use https unless allow_insecure_http is explicitly enabled")
		}
	}
	if strings.TrimSpace(c.DeviceID) == "" {
		return errors.New("device_id is required")
	}
	if requireToken && strings.TrimSpace(c.TokenFile) == "" {
		return errors.New("token_file is required")
	}
	if strings.TrimSpace(c.TokenFile) != "" {
		c.TokenFile = filepath.Clean(c.TokenFile)
		if !filepath.IsAbs(c.TokenFile) {
			return errors.New("token_file must be absolute")
		}
	}
	if c.ReadyFile != "" {
		c.ReadyFile = filepath.Clean(c.ReadyFile)
		if !filepath.IsAbs(c.ReadyFile) {
			return errors.New("COMMA_COMPANION_READY_FILE must be absolute")
		}
		if err := rejectExistingSymlinkPath(c.ReadyFile); err != nil {
			return fmt.Errorf("COMMA_COMPANION_READY_FILE: %w", err)
		}
	}
	c.Commands.ControlSocket = strings.TrimSpace(c.Commands.ControlSocket)
	if c.Commands.ControlSocket != "" {
		c.Commands.ControlSocket = filepath.Clean(c.Commands.ControlSocket)
		if !filepath.IsAbs(c.Commands.ControlSocket) {
			return errors.New("commands.control_socket must be absolute")
		}
		if err := rejectExistingSymlinkPath(c.Commands.ControlSocket); err != nil {
			return fmt.Errorf("commands.control_socket: %w", err)
		}
	}
	privilegedControl := c.Commands.AllowStarPilotRestart || c.Commands.AllowPowerCommands
	if privilegedControl && c.Commands.ControlSocket == "" {
		return errors.New("enabled privileged commands require commands.control_socket")
	}
	if privilegedControl && requireToken && strings.TrimSpace(c.ControlTokenFile) == "" {
		return errors.New(
			"enabled privileged commands require COMMA_COMPANION_CONTROL_TOKEN_FILE",
		)
	}
	if c.ControlTokenFile != "" {
		c.ControlTokenFile = filepath.Clean(c.ControlTokenFile)
		if !filepath.IsAbs(c.ControlTokenFile) {
			return errors.New("COMMA_COMPANION_CONTROL_TOKEN_FILE must be absolute")
		}
	}
	if len(c.Roots) == 0 {
		return errors.New("at least one root is required")
	}
	seenRoots := make(map[string]bool)
	seenPaths := make(map[string]bool)
	for i := range c.Roots {
		c.Roots[i].Name = strings.TrimSpace(c.Roots[i].Name)
		c.Roots[i].Path = filepath.Clean(c.Roots[i].Path)
		if c.Roots[i].Name == "" || c.Roots[i].Path == "." {
			return fmt.Errorf("roots[%d] requires name and path", i)
		}
		if !filepath.IsAbs(c.Roots[i].Path) {
			return fmt.Errorf("roots[%d].path must be absolute", i)
		}
		if err := rejectExistingSymlinkPath(c.Roots[i].Path); err != nil {
			return fmt.Errorf("roots[%d].path: %w", i, err)
		}
		if !validName(c.Roots[i].Name) {
			return fmt.Errorf("roots[%d].name may contain only letters, digits, dot, underscore, and dash", i)
		}
		if len(c.Roots[i].Name) > 64 {
			return fmt.Errorf("roots[%d].name exceeds 64 bytes", i)
		}
		if seenRoots[c.Roots[i].Name] {
			return fmt.Errorf("duplicate root name %q", c.Roots[i].Name)
		}
		if seenPaths[c.Roots[i].Path] {
			return fmt.Errorf("duplicate root path %q", c.Roots[i].Path)
		}
		seenRoots[c.Roots[i].Name] = true
		seenPaths[c.Roots[i].Path] = true
	}
	seenStreams := make(map[string]bool)
	inventoryRoots := make(map[string]bool)
	rlogRoots := make(map[string]bool)
	for i := range c.Inventory.ExpectedStreams {
		stream := &c.Inventory.ExpectedStreams[i]
		stream.RootName = strings.TrimSpace(stream.RootName)
		stream.ArtifactType = strings.ToLower(strings.TrimSpace(stream.ArtifactType))
		stream.Camera = strings.ToLower(strings.TrimSpace(stream.Camera))
		if !seenRoots[stream.RootName] {
			return fmt.Errorf(
				"inventory.expected_streams[%d].root_name must name a configured root",
				i,
			)
		}
		switch stream.ArtifactType {
		case "video":
			if !validName(stream.Camera) || len(stream.Camera) > 32 {
				return fmt.Errorf(
					"inventory.expected_streams[%d].camera is required for video",
					i,
				)
			}
		case "rlog", "qlog":
			if stream.Camera != "" {
				return fmt.Errorf(
					"inventory.expected_streams[%d].camera must be empty for logs",
					i,
				)
			}
			if stream.ArtifactType == "rlog" {
				rlogRoots[stream.RootName] = true
			}
		default:
			return fmt.Errorf(
				"inventory.expected_streams[%d].artifact_type must be video, rlog, or qlog",
				i,
			)
		}
		inventoryRoots[stream.RootName] = true
		role := inventory.StreamRole(stream.RootName, stream.ArtifactType, stream.Camera)
		if seenStreams[role] {
			return fmt.Errorf("duplicate inventory expected stream %q", role)
		}
		seenStreams[role] = true
	}
	for rootName := range inventoryRoots {
		if !rlogRoots[rootName] {
			return fmt.Errorf(
				"inventory.expected_streams profile for root %q must include rlog",
				rootName,
			)
		}
	}
	if c.SpoolDir == "" {
		return errors.New("spool_dir is required")
	}
	c.SpoolDir = filepath.Clean(c.SpoolDir)
	if !filepath.IsAbs(c.SpoolDir) {
		return errors.New("spool_dir must be absolute")
	}
	if err := rejectExistingSymlinkPath(c.SpoolDir); err != nil {
		return fmt.Errorf("spool_dir: %w", err)
	}
	if c.JournalPath == "" {
		c.JournalPath = filepath.Join(c.SpoolDir, "journal.json")
	}
	c.JournalPath = filepath.Clean(c.JournalPath)
	if !filepath.IsAbs(c.JournalPath) {
		return errors.New("journal_path must be absolute")
	}
	if err := rejectExistingSymlinkPath(c.JournalPath); err != nil {
		return fmt.Errorf("journal_path: %w", err)
	}
	journalRelative, err := filepath.Rel(c.SpoolDir, c.JournalPath)
	if err != nil || journalRelative == "." || journalRelative == ".." ||
		strings.HasPrefix(journalRelative, ".."+string(filepath.Separator)) {
		return errors.New("journal_path must be a file inside spool_dir")
	}
	for _, root := range c.Roots {
		relative, err := filepath.Rel(root.Path, c.SpoolDir)
		if err == nil && relative != ".." && !strings.HasPrefix(relative, ".."+string(filepath.Separator)) {
			return fmt.Errorf("spool_dir must be outside logging root %q", root.Name)
		}
		journalRelative, err := filepath.Rel(root.Path, c.JournalPath)
		if err == nil && journalRelative != ".." &&
			!strings.HasPrefix(journalRelative, ".."+string(filepath.Separator)) {
			return fmt.Errorf("journal_path must be outside logging root %q", root.Name)
		}
	}
	if c.StableObservations < 2 {
		return errors.New("stable_observations must be at least 2")
	}
	if c.StableDuration.Duration <= 0 {
		return errors.New("stable_duration must be positive")
	}
	if c.ChunkSize <= 0 || c.ChunkSize > 64*1024*1024 {
		return errors.New("chunk_size must be between 1 byte and 64 MiB")
	}
	if c.MaxFilesPerScan <= 0 || c.MaxFilesPerScan > 1000 {
		return errors.New("max_files_per_scan must be between 1 and 1000")
	}
	if c.MaxConcurrentUploads != 1 {
		return errors.New("max_concurrent_uploads must currently be 1")
	}
	for name, duration := range map[string]time.Duration{
		"scan_interval":         c.ScanInterval.Duration,
		"upload_poll_interval":  c.UploadPollInterval.Duration,
		"heartbeat_interval":    c.HeartbeatInterval.Duration,
		"long_poll_timeout":     c.LongPollTimeout.Duration,
		"http_timeout":          c.HTTPTimeout.Duration,
		"stable_duration":       c.StableDuration.Duration,
		"final_segment_grace":   c.FinalSegmentGrace.Duration,
		"offroad_segment_grace": c.OffroadSegmentGrace.Duration,
		"non_segment_grace":     c.NonSegmentGrace.Duration,
	} {
		if duration <= 0 {
			return fmt.Errorf("%s must be positive", name)
		}
	}
	if c.LongPollTimeout.Duration >= c.HTTPTimeout.Duration {
		return errors.New("long_poll_timeout must be shorter than http_timeout")
	}
	if c.HeartbeatInterval.Duration < 5*time.Second {
		return errors.New("heartbeat_interval must be at least 5s")
	}
	if len(c.Policy.TrueValues) == 0 {
		c.Policy.TrueValues = []string{"1", "true", "yes", "on"}
	}
	if c.Policy.OffroadStableDuration.Duration <= 0 {
		return errors.New("policy.offroad_stable_duration must be positive")
	}
	if c.Policy.OffroadMaxAge.Duration < 0 {
		return errors.New("policy.offroad_max_age must be non-negative")
	}
	if c.Storage.MaxRetainedBytes <= 0 {
		return errors.New("storage.max_retained_bytes must be positive")
	}
	if c.Storage.MinFreeBytes <= 0 {
		return errors.New("storage.min_free_bytes must be positive")
	}
	if c.Storage.EmergencyBehavior != "release_oldest" && c.Storage.EmergencyBehavior != "pause" {
		return errors.New("storage.emergency_behavior must be release_oldest or pause")
	}
	return nil
}

func (c Config) ValidateDevicePaths() error {
	if runtime.GOOS != "linux" {
		return nil
	}
	for index, root := range c.Roots {
		if !approvedLinuxLoggingRoot(root.Path) {
			return fmt.Errorf(
				"roots[%d].path must be a direct /data/media/0/realdata* directory",
				index,
			)
		}
		if err := rejectExistingSymlinkPath(root.Path); err != nil {
			return fmt.Errorf("roots[%d].path: %w", index, err)
		}
	}
	return nil
}

func ensureJSONEOF(decoder *json.Decoder) error {
	var trailing any
	if err := decoder.Decode(&trailing); !errors.Is(err, io.EOF) {
		if err == nil {
			return errors.New("config contains more than one JSON value")
		}
		return err
	}
	return nil
}

func readTokenFile(path string) (string, error) {
	pathInfo, err := os.Lstat(path)
	if err != nil {
		return "", fmt.Errorf("inspect token file: %w", err)
	}
	if pathInfo.Mode()&os.ModeSymlink != 0 || !pathInfo.Mode().IsRegular() {
		return "", errors.New("token_file must be a regular non-symlink file")
	}
	handle, err := os.Open(path)
	if err != nil {
		return "", fmt.Errorf("open token file: %w", err)
	}
	defer handle.Close()
	info, err := handle.Stat()
	if err != nil {
		return "", fmt.Errorf("inspect opened token file: %w", err)
	}
	if !info.Mode().IsRegular() || !os.SameFile(pathInfo, info) {
		return "", errors.New("token_file changed identity while opening")
	}
	if info.Size() < 16 || info.Size() > 4096 {
		return "", errors.New("token_file must contain between 16 and 4096 bytes")
	}
	permissions := info.Mode().Perm()
	if runtime.GOOS == "linux" && permissions != 0o400 && permissions != 0o600 {
		return "", fmt.Errorf("token_file permissions must be 0400 or 0600, got %04o", permissions)
	}
	if err := validateTokenOwner(info); err != nil {
		return "", err
	}
	data, err := io.ReadAll(io.LimitReader(handle, 4097))
	if err != nil {
		return "", fmt.Errorf("read token file: %w", err)
	}
	finalInfo, err := handle.Stat()
	if err != nil {
		return "", fmt.Errorf("reinspect token file: %w", err)
	}
	if !os.SameFile(info, finalInfo) ||
		info.Size() != finalInfo.Size() ||
		info.ModTime() != finalInfo.ModTime() {
		return "", errors.New("token_file changed while reading")
	}
	if len(data) > 4096 {
		return "", errors.New("token_file must contain between 16 and 4096 bytes")
	}
	token := strings.TrimSpace(string(data))
	if len(token) < 16 || len(token) > 4096 {
		return "", errors.New("device token must contain between 16 and 4096 non-whitespace bytes")
	}
	for _, character := range token {
		if character <= 0x20 || character == 0x7f {
			return "", errors.New("device token may not contain whitespace or control characters")
		}
	}
	return token, nil
}

func approvedLinuxLoggingRoot(path string) bool {
	clean := filepath.Clean(path)
	if filepath.Dir(clean) != "/data/media/0" {
		return false
	}
	return strings.HasPrefix(filepath.Base(clean), "realdata")
}

func rejectExistingSymlinkPath(path string) error {
	existing := filepath.Clean(path)
	for {
		_, err := os.Lstat(existing)
		if err == nil {
			break
		}
		if !errors.Is(err, os.ErrNotExist) {
			return err
		}
		parent := filepath.Dir(existing)
		if parent == existing {
			return nil
		}
		existing = parent
	}
	resolved, err := filepath.EvalSymlinks(existing)
	if err != nil {
		return err
	}
	absolute, err := filepath.Abs(existing)
	if err != nil {
		return err
	}
	resolvedAbsolute, err := filepath.Abs(resolved)
	if err != nil {
		return err
	}
	if filepath.Clean(absolute) != filepath.Clean(resolvedAbsolute) {
		return errors.New("path or a parent component is a symlink")
	}
	return nil
}

func validName(value string) bool {
	for _, character := range value {
		if (character < 'a' || character > 'z') &&
			(character < 'A' || character > 'Z') &&
			(character < '0' || character > '9') &&
			character != '.' && character != '_' && character != '-' {
			return false
		}
	}
	return value != ""
}

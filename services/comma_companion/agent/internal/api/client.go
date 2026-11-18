package api

import (
	"bytes"
	"context"
	"crypto/sha256"
	"crypto/tls"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"

	inventorycontract "starpilot.local/comma-companion-agent/internal/inventory"
	"starpilot.local/comma-companion-agent/internal/state"
)

var ErrNotFound = errors.New("resource not found")

type Client struct {
	baseURL   string
	deviceID  string
	token     string
	userAgent string
	client    *http.Client
}

type UploadDeclaration struct {
	FileID             string   `json:"file_id"`
	DeviceID           string   `json:"device_id"`
	RouteName          string   `json:"route_name,omitempty"`
	SegmentNumber      *int     `json:"segment_number,omitempty"`
	ArtifactType       string   `json:"artifact_type"`
	Camera             string   `json:"camera,omitempty"`
	RelativePath       string   `json:"relative_path"`
	Size               int64    `json:"size"`
	ModTimeNS          int64    `json:"mtime_ns"`
	SHA256             string   `json:"sha256"`
	CompletionEvidence []string `json:"completion_evidence,omitempty"`
	Partial            bool     `json:"partial"`
}

type UploadStatus struct {
	ID          string `json:"upload_id"`
	Offset      int64  `json:"offset"`
	Length      int64  `json:"length"`
	State       string `json:"state"`
	Durable     bool   `json:"durable"`
	Terminal    bool   `json:"terminal"`
	RetryAction string `json:"retry_action,omitempty"`
	SHA256      string `json:"sha256,omitempty"`
}

func (s UploadStatus) NeedsRedeclare() bool {
	return !s.Durable && (strings.EqualFold(s.State, "failed") ||
		strings.EqualFold(s.State, "canceled") ||
		(s.Terminal && strings.EqualFold(s.RetryAction, "redeclare")))
}

func (s UploadStatus) IsCanceled() bool {
	return strings.EqualFold(s.State, "canceled")
}

type Command struct {
	ID              string         `json:"id"`
	DeviceID        string         `json:"device_id"`
	Type            string         `json:"type"`
	State           string         `json:"state"`
	IssuedAt        time.Time      `json:"issued_at"`
	ExpiresAt       time.Time      `json:"expires_at"`
	RequiresOffroad bool           `json:"requires_offroad"`
	Args            map[string]any `json:"args"`
	DeliveredAt     *time.Time     `json:"delivered_at"`
	StartedAt       *time.Time     `json:"started_at"`
	FinishedAt      *time.Time     `json:"finished_at"`
	Message         *string        `json:"message"`
	Error           *string        `json:"error"`
}

type HeartbeatRequest struct {
	AgentVersion    string         `json:"agent_version"`
	Timestamp       time.Time      `json:"timestamp"`
	State           string         `json:"state"`
	Capabilities    []string       `json:"capabilities"`
	Metrics         map[string]any `json:"metrics"`
	Offroad         *bool          `json:"offroad,omitempty"`
	NetworkType     string         `json:"network_type"`
	SoftwareVersion string         `json:"software_version,omitempty"`
}

type HeartbeatResponse struct {
	ServerTime time.Time `json:"server_time"`
	Commands   []Command `json:"commands"`
}

type CommandResult struct {
	State      string     `json:"state"`
	StartedAt  *time.Time `json:"started_at,omitempty"`
	FinishedAt *time.Time `json:"finished_at,omitempty"`
	Message    string     `json:"message,omitempty"`
	Error      string     `json:"error,omitempty"`
}

type RouteInventoryAcceptance struct {
	ManifestSHA256 string `json:"manifest_sha256"`
	Generation     int    `json:"generation"`
	State          string `json:"state"`
}

type routeInventoryEnvelope struct {
	ManifestSHA256 string              `json:"manifest_sha256"`
	Manifest       state.RouteManifest `json:"manifest"`
}

type statusError struct {
	Code int
	Body string
}

func (e *statusError) Error() string {
	return fmt.Sprintf("server returned HTTP %d: %s", e.Code, e.Body)
}

func New(baseURL, deviceID, token, userAgent string, timeout time.Duration) *Client {
	transport := &http.Transport{
		Proxy:               http.ProxyFromEnvironment,
		TLSClientConfig:     &tls.Config{MinVersion: tls.VersionTLS12},
		MaxIdleConns:        4,
		MaxIdleConnsPerHost: 2,
		IdleConnTimeout:     90 * time.Second,
	}
	return &Client{
		baseURL:   strings.TrimRight(baseURL, "/"),
		deviceID:  deviceID,
		token:     token,
		userAgent: userAgent,
		client: &http.Client{
			Timeout:   timeout,
			Transport: transport,
			CheckRedirect: func(_ *http.Request, _ []*http.Request) error {
				return errors.New("HTTP redirects are disabled")
			},
		},
	}
}

func (c *Client) CreateUpload(ctx context.Context, file state.File) (UploadStatus, error) {
	if !validAgentFileID(file.ID) {
		return UploadStatus{}, errors.New(
			"agent file_id must be 64 lowercase SHA-256 hexadecimal characters",
		)
	}
	declaration := UploadDeclaration{
		FileID:             file.ID,
		DeviceID:           c.deviceID,
		RouteName:          file.RouteName,
		SegmentNumber:      file.SegmentNumber,
		ArtifactType:       file.ArtifactType,
		Camera:             file.Camera,
		RelativePath:       file.RelativePath,
		Size:               file.Size,
		ModTimeNS:          file.ModTimeNS,
		SHA256:             file.SHA256,
		CompletionEvidence: append([]string(nil), file.CompletionEvidence...),
		Partial:            file.Partial,
	}
	var response uploadResponse
	idempotencyKey := "upload-create:" + file.ID + ":" + strconv.Itoa(file.UploadAttempt)
	if err := c.jsonRequest(ctx, http.MethodPost, "/api/v1/uploads", declaration, &response, idempotencyKey); err != nil {
		return UploadStatus{}, err
	}
	return response.status(), nil
}

func validAgentFileID(value string) bool {
	if len(value) != sha256.Size*2 || value != strings.ToLower(value) {
		return false
	}
	decoded, err := hex.DecodeString(value)
	return err == nil && len(decoded) == sha256.Size
}

func (c *Client) HeadUpload(ctx context.Context, id string) (UploadStatus, error) {
	request, err := c.request(ctx, http.MethodHead, "/api/v1/uploads/"+url.PathEscape(id), nil)
	if err != nil {
		return UploadStatus{}, err
	}
	response, err := c.client.Do(request)
	if err != nil {
		return UploadStatus{}, err
	}
	defer response.Body.Close()
	if response.StatusCode == http.StatusNotFound {
		return UploadStatus{}, ErrNotFound
	}
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		return UploadStatus{}, responseError(response, c.token)
	}
	offset, err := requiredIntHeader(response.Header, "Upload-Offset")
	if err != nil {
		return UploadStatus{}, err
	}
	length, err := requiredIntHeader(response.Header, "Upload-Length")
	if err != nil {
		return UploadStatus{}, err
	}
	durable, _ := strconv.ParseBool(response.Header.Get("Upload-Durable"))
	terminal, _ := strconv.ParseBool(response.Header.Get("Upload-Terminal"))
	return UploadStatus{
		ID:          id,
		Offset:      offset,
		Length:      length,
		State:       response.Header.Get("Upload-State"),
		Durable:     durable || terminalUploadState(response.Header.Get("Upload-State")),
		Terminal:    terminal || anyTerminalUploadState(response.Header.Get("Upload-State")),
		RetryAction: response.Header.Get("Upload-Retry-Action"),
		SHA256:      response.Header.Get("Upload-SHA256"),
	}, nil
}

func (c *Client) PatchUpload(ctx context.Context, id string, offset, total int64, chunk []byte) (UploadStatus, error) {
	request, err := c.request(
		ctx,
		http.MethodPatch,
		"/api/v1/uploads/"+url.PathEscape(id),
		bytes.NewReader(chunk),
	)
	if err != nil {
		return UploadStatus{}, err
	}
	digest := sha256.Sum256(chunk)
	request.Header.Set("Content-Type", "application/offset+octet-stream")
	request.Header.Set("Upload-Offset", strconv.FormatInt(offset, 10))
	request.Header.Set("Upload-Length", strconv.FormatInt(total, 10))
	request.Header.Set("Upload-Checksum", "sha256 "+base64.StdEncoding.EncodeToString(digest[:]))
	request.ContentLength = int64(len(chunk))
	response, err := c.client.Do(request)
	if err != nil {
		return UploadStatus{}, err
	}
	defer response.Body.Close()
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		return UploadStatus{}, responseError(response, c.token)
	}
	var decoded uploadResponse
	if response.ContentLength != 0 {
		_ = json.NewDecoder(io.LimitReader(response.Body, 1<<20)).Decode(&decoded)
	}
	status := decoded.status()
	if status.ID == "" {
		status.ID = id
	}
	if value := response.Header.Get("Upload-Offset"); value != "" {
		status.Offset, _ = strconv.ParseInt(value, 10, 64)
	}
	if value := response.Header.Get("Upload-Length"); value != "" {
		status.Length, _ = strconv.ParseInt(value, 10, 64)
	}
	if value := response.Header.Get("Upload-State"); value != "" {
		status.State = value
	}
	if value := response.Header.Get("Upload-SHA256"); value != "" {
		status.SHA256 = value
	}
	if value := response.Header.Get("Upload-Durable"); value != "" {
		status.Durable, _ = strconv.ParseBool(value)
	}
	if value := response.Header.Get("Upload-Terminal"); value != "" {
		status.Terminal, _ = strconv.ParseBool(value)
	}
	if value := response.Header.Get("Upload-Retry-Action"); value != "" {
		status.RetryAction = value
	}
	if terminalUploadState(status.State) {
		status.Durable = true
	}
	if anyTerminalUploadState(status.State) {
		status.Terminal = true
	}
	return status, nil
}

func (c *Client) CancelUpload(ctx context.Context, file state.File) (UploadStatus, error) {
	if file.UploadID == "" {
		return UploadStatus{}, errors.New("cannot cancel upload without an upload ID")
	}
	path := "/api/v1/uploads/" + url.PathEscape(file.UploadID) + "/cancel"
	var response uploadResponse
	key := "upload-cancel:" + file.ID + ":" + file.UploadID
	if err := c.jsonRequest(ctx, http.MethodPost, path, nil, &response, key); err != nil {
		var serverError *statusError
		if errors.As(err, &serverError) && serverError.Code == http.StatusNotFound {
			return UploadStatus{}, ErrNotFound
		}
		return UploadStatus{}, err
	}
	return response.status(), nil
}

func (c *Client) DeclareRouteInventory(
	ctx context.Context,
	record state.RouteInventory,
) (RouteInventoryAcceptance, error) {
	manifest := record.Manifest
	if err := inventorycontract.ValidateAndNormalize(&manifest); err != nil {
		return RouteInventoryAcceptance{}, fmt.Errorf("validate route inventory before declaration: %w", err)
	}
	_, manifestSHA256, err := inventorycontract.CanonicalManifest(manifest)
	if err != nil {
		return RouteInventoryAcceptance{}, err
	}
	if record.ManifestSHA256 != manifestSHA256 {
		return RouteInventoryAcceptance{}, fmt.Errorf(
			"route inventory journal digest %s does not match canonical manifest %s",
			record.ManifestSHA256,
			manifestSHA256,
		)
	}
	envelope := routeInventoryEnvelope{
		ManifestSHA256: record.ManifestSHA256,
		Manifest:       manifest,
	}
	var response RouteInventoryAcceptance
	err = c.jsonRequestStrictStatus(
		ctx,
		http.MethodPost,
		"/api/v1/route-inventories",
		envelope,
		&response,
		"route-inventory:"+record.ManifestSHA256,
		http.StatusOK,
		http.StatusCreated,
	)
	if err != nil {
		return RouteInventoryAcceptance{}, err
	}
	if response.State != "accepted" ||
		response.ManifestSHA256 != record.ManifestSHA256 ||
		response.Generation != record.Manifest.Generation {
		return RouteInventoryAcceptance{}, errors.New(
			"route inventory acceptance did not echo the exact manifest generation and digest",
		)
	}
	return response, nil
}

func (c *Client) Heartbeat(ctx context.Context, wait time.Duration, heartbeat HeartbeatRequest) (HeartbeatResponse, error) {
	path := "/api/v1/devices/" + url.PathEscape(c.deviceID) + "/heartbeat?wait_seconds=" +
		strconv.Itoa(int(wait.Round(time.Second)/time.Second))
	var response HeartbeatResponse
	err := c.jsonRequestStrict(ctx, http.MethodPost, path, heartbeat, &response, "")
	if err != nil {
		return HeartbeatResponse{}, err
	}
	if response.ServerTime.IsZero() {
		return HeartbeatResponse{}, errors.New("heartbeat response lacks server_time")
	}
	if len(response.Commands) > 32 {
		return HeartbeatResponse{}, fmt.Errorf(
			"heartbeat command batch exceeds local limit: %d > 32",
			len(response.Commands),
		)
	}
	for _, command := range response.Commands {
		if command.DeviceID != c.deviceID {
			return HeartbeatResponse{}, errors.New("heartbeat command device_id does not match this device")
		}
	}
	return response, err
}

func (c *Client) ReportCommand(ctx context.Context, id string, result CommandResult) error {
	path := "/api/v1/devices/" + url.PathEscape(c.deviceID) + "/commands/" + url.PathEscape(id) + "/result"
	return c.jsonRequest(ctx, http.MethodPost, path, result, nil, "command-result:"+id+":"+result.State)
}

func (c *Client) jsonRequest(ctx context.Context, method, path string, input, output any, idempotencyKey string) error {
	return c.jsonRequestWithMode(ctx, method, path, input, output, idempotencyKey, false, nil)
}

func (c *Client) jsonRequestStrict(
	ctx context.Context,
	method, path string,
	input, output any,
	idempotencyKey string,
) error {
	return c.jsonRequestWithMode(ctx, method, path, input, output, idempotencyKey, true, nil)
}

func (c *Client) jsonRequestStrictStatus(
	ctx context.Context,
	method, path string,
	input, output any,
	idempotencyKey string,
	allowedStatuses ...int,
) error {
	return c.jsonRequestWithMode(
		ctx,
		method,
		path,
		input,
		output,
		idempotencyKey,
		true,
		allowedStatuses,
	)
}

func (c *Client) jsonRequestWithMode(
	ctx context.Context,
	method, path string,
	input, output any,
	idempotencyKey string,
	strict bool,
	allowedStatuses []int,
) error {
	var body io.Reader
	if input != nil {
		encoded, err := json.Marshal(input)
		if err != nil {
			return err
		}
		body = bytes.NewReader(encoded)
	}
	request, err := c.request(ctx, method, path, body)
	if err != nil {
		return err
	}
	if input != nil {
		request.Header.Set("Content-Type", "application/json")
	}
	if idempotencyKey != "" {
		request.Header.Set("Idempotency-Key", idempotencyKey)
	}
	response, err := c.client.Do(request)
	if err != nil {
		return err
	}
	defer response.Body.Close()
	statusAllowed := response.StatusCode >= 200 && response.StatusCode < 300
	if len(allowedStatuses) > 0 {
		statusAllowed = false
		for _, allowed := range allowedStatuses {
			if response.StatusCode == allowed {
				statusAllowed = true
				break
			}
		}
	}
	if !statusAllowed {
		return responseError(response, c.token)
	}
	if output == nil || response.StatusCode == http.StatusNoContent || response.ContentLength == 0 {
		return nil
	}
	decoder := json.NewDecoder(io.LimitReader(response.Body, 2<<20))
	if strict {
		decoder.DisallowUnknownFields()
	}
	if err := decoder.Decode(output); err != nil {
		return fmt.Errorf("decode server response: %w", err)
	}
	var trailing any
	if err := decoder.Decode(&trailing); !errors.Is(err, io.EOF) {
		if err == nil {
			return errors.New("decode server response: multiple JSON values")
		}
		return fmt.Errorf("decode server response trailing data: %w", err)
	}
	return nil
}

func (c *Client) request(ctx context.Context, method, path string, body io.Reader) (*http.Request, error) {
	request, err := http.NewRequestWithContext(ctx, method, c.baseURL+path, body)
	if err != nil {
		return nil, err
	}
	request.Header.Set("Authorization", "Bearer "+c.token)
	request.Header.Set("Accept", "application/json")
	request.Header.Set("User-Agent", c.userAgent)
	return request, nil
}

func responseError(response *http.Response, token string) error {
	if response.StatusCode == http.StatusUnauthorized || response.StatusCode == http.StatusForbidden {
		return &statusError{Code: response.StatusCode, Body: "authentication or authorization failed"}
	}
	body, _ := io.ReadAll(io.LimitReader(response.Body, 4<<10))
	message := strings.NewReplacer("\r", " ", "\n", " ").Replace(strings.TrimSpace(string(body)))
	if token != "" {
		message = strings.ReplaceAll(message, token, "[redacted]")
	}
	return &statusError{Code: response.StatusCode, Body: message}
}

func requiredIntHeader(headers http.Header, name string) (int64, error) {
	value := headers.Get(name)
	if value == "" {
		return 0, fmt.Errorf("server response lacks %s", name)
	}
	parsed, err := strconv.ParseInt(value, 10, 64)
	if err != nil {
		return 0, fmt.Errorf("invalid %s: %w", name, err)
	}
	return parsed, nil
}

type uploadResponse struct {
	ID       string `json:"id"`
	UploadID string `json:"upload_id"`
	Offset   int64  `json:"offset"`
	Length   int64  `json:"length"`
	Status   string `json:"status"`
	State    string `json:"state"`
	Durable  bool   `json:"durable"`
	SHA256   string `json:"sha256"`
}

func (r uploadResponse) status() UploadStatus {
	id := r.UploadID
	if id == "" {
		id = r.ID
	}
	stateValue := r.State
	if stateValue == "" {
		stateValue = r.Status
	}
	return UploadStatus{
		ID:       id,
		Offset:   r.Offset,
		Length:   r.Length,
		State:    stateValue,
		Durable:  r.Durable || terminalUploadState(stateValue),
		Terminal: anyTerminalUploadState(stateValue),
		SHA256:   r.SHA256,
	}
}

func terminalUploadState(value string) bool {
	return strings.EqualFold(value, "durable") || strings.EqualFold(value, "complete")
}

func anyTerminalUploadState(value string) bool {
	return terminalUploadState(value) ||
		strings.EqualFold(value, "failed") ||
		strings.EqualFold(value, "canceled")
}

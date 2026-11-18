package api

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	inventorycontract "starpilot.local/comma-companion-agent/internal/inventory"
	"starpilot.local/comma-companion-agent/internal/state"
)

const apiTestFileID = "1111111111111111111111111111111111111111111111111111111111111111"

func TestHeadTreatsCompleteStateAsDurable(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		response.Header().Set("Upload-Offset", "12")
		response.Header().Set("Upload-Length", "12")
		response.Header().Set("Upload-State", "complete")
		response.Header().Set("Upload-SHA256", "abcd")
		response.WriteHeader(http.StatusNoContent)
	}))
	defer server.Close()
	client := New(server.URL, "device", "token", "test", time.Second)
	status, err := client.HeadUpload(context.Background(), "upload")
	if err != nil {
		t.Fatal(err)
	}
	if !status.Durable || status.Offset != 12 || status.Length != 12 || status.SHA256 != "abcd" {
		t.Fatalf("unexpected status: %#v", status)
	}
}

func TestHeadRecognizesFailedAndCanceledAsRedeclareTerminals(t *testing.T) {
	for _, stateValue := range []string{"failed", "canceled"} {
		t.Run(stateValue, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
				response.Header().Set("Upload-Offset", "7")
				response.Header().Set("Upload-Length", "12")
				response.Header().Set("Upload-State", stateValue)
				response.Header().Set("Upload-Durable", "false")
				response.Header().Set("Upload-Terminal", "true")
				response.Header().Set("Upload-Retry-Action", "redeclare")
				response.WriteHeader(http.StatusNoContent)
			}))
			defer server.Close()
			client := New(server.URL, "device", "token", "test", time.Second)
			status, err := client.HeadUpload(context.Background(), "upload")
			if err != nil {
				t.Fatal(err)
			}
			if status.Durable || !status.Terminal || !status.NeedsRedeclare() ||
				status.RetryAction != "redeclare" {
				t.Fatalf("unexpected status: %#v", status)
			}
		})
	}
}

func TestCreateAndCancelUseAttemptScopedIdempotency(t *testing.T) {
	var createKey, cancelKey string
	var declared UploadDeclaration
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		switch request.URL.Path {
		case "/api/v1/uploads":
			createKey = request.Header.Get("Idempotency-Key")
			if err := json.NewDecoder(request.Body).Decode(&declared); err != nil {
				t.Error(err)
			}
			_, _ = response.Write([]byte(`{"upload_id":"session","state":"receiving","length":1}`))
		case "/api/v1/uploads/session/cancel":
			cancelKey = request.Header.Get("Idempotency-Key")
			_, _ = response.Write([]byte(`{"upload_id":"session","state":"canceled","length":1}`))
		default:
			http.NotFound(response, request)
		}
	}))
	defer server.Close()
	client := New(server.URL, "device", "token", "test", time.Second)
	file := state.File{ID: apiTestFileID, UploadID: "session", UploadAttempt: 3, Size: 1}
	if _, err := client.CreateUpload(context.Background(), file); err != nil {
		t.Fatal(err)
	}
	status, err := client.CancelUpload(context.Background(), file)
	if err != nil {
		t.Fatal(err)
	}
	if createKey != "upload-create:"+apiTestFileID+":3" ||
		cancelKey != "upload-cancel:"+apiTestFileID+":session" {
		t.Fatalf("unexpected keys: create=%q cancel=%q", createKey, cancelKey)
	}
	if declared.FileID != apiTestFileID || declared.DeviceID != "device" {
		t.Fatalf("declaration lost stable file selector: %#v", declared)
	}
	if !status.IsCanceled() {
		t.Fatalf("cancel response was not recognized: %#v", status)
	}
}

func TestCreateUploadRejectsInvalidAgentFileIDBeforeNetwork(t *testing.T) {
	requests := 0
	server := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
		requests++
	}))
	defer server.Close()
	client := New(server.URL, "device", "token", "test", time.Second)
	for _, fileID := range []string{"", "file", strings.Repeat("A", 64), strings.Repeat("z", 64)} {
		if _, err := client.CreateUpload(
			context.Background(),
			state.File{ID: fileID},
		); err == nil || !strings.Contains(err.Error(), "file_id") {
			t.Fatalf("invalid file_id %q was accepted: %v", fileID, err)
		}
	}
	if requests != 0 {
		t.Fatalf("invalid file ID reached the server %d time(s)", requests)
	}
}

func TestHeartbeatStrictlyRejectsUnknownResponseFields(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		_, _ = response.Write([]byte(`{
			"server_time":"2026-07-29T12:00:00Z",
			"commands":[],
			"unexpected":true
		}`))
	}))
	defer server.Close()
	client := New(server.URL, "device", "token", "test", time.Second)
	if _, err := client.Heartbeat(
		context.Background(),
		time.Second,
		HeartbeatRequest{Timestamp: time.Now().UTC()},
	); err == nil {
		t.Fatal("unknown heartbeat response field was accepted")
	}
}

func TestHeartbeatBindsCommandBatchToDeviceAndLimit(t *testing.T) {
	for _, test := range []struct {
		name     string
		count    int
		deviceID string
	}{
		{name: "wrong-device", count: 1, deviceID: "other"},
		{name: "oversized-batch", count: 33, deviceID: "device"},
	} {
		t.Run(test.name, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
				commands := make([]map[string]any, test.count)
				now := time.Now().UTC()
				for index := range commands {
					commands[index] = map[string]any{
						"id":               "0123456789abcdef0123456789abcdef",
						"device_id":        test.deviceID,
						"type":             "status",
						"args":             map[string]any{},
						"state":            "delivered",
						"requires_offroad": false,
						"issued_at":        now,
						"expires_at":       now.Add(time.Minute),
						"delivered_at":     now,
						"started_at":       nil,
						"finished_at":      nil,
						"message":          nil,
						"error":            nil,
					}
				}
				_ = json.NewEncoder(response).Encode(map[string]any{
					"server_time": now,
					"commands":    commands,
				})
			}))
			defer server.Close()
			client := New(server.URL, "device", "token", "test", time.Second)
			if _, err := client.Heartbeat(
				context.Background(),
				time.Second,
				HeartbeatRequest{Timestamp: time.Now().UTC()},
			); err == nil {
				t.Fatal("unbound heartbeat command batch was accepted")
			}
		})
	}
}

func TestDeclareRouteInventoryUsesImmutableContractAndExactReplay(t *testing.T) {
	record := apiTestRouteInventory(t)
	requestCount := 0
	var firstBody []byte
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		requestCount++
		if request.Method != http.MethodPost || request.URL.Path != "/api/v1/route-inventories" {
			http.Error(response, "wrong route", http.StatusNotFound)
			return
		}
		if request.Header.Get("Authorization") != "Bearer token" {
			http.Error(response, "wrong auth", http.StatusUnauthorized)
			return
		}
		if request.Header.Get("Idempotency-Key") != "route-inventory:"+record.ManifestSHA256 {
			http.Error(response, "wrong idempotency key", http.StatusBadRequest)
			return
		}
		raw, err := io.ReadAll(request.Body)
		if err != nil {
			t.Errorf("read request: %v", err)
			return
		}
		if requestCount == 1 {
			firstBody = raw
			response.WriteHeader(http.StatusCreated)
		} else {
			if string(raw) != string(firstBody) {
				t.Errorf("idempotent replay body changed")
			}
			response.WriteHeader(http.StatusOK)
		}
		_ = json.NewEncoder(response).Encode(RouteInventoryAcceptance{
			ManifestSHA256: record.ManifestSHA256,
			Generation:     1,
			State:          "accepted",
		})
	}))
	defer server.Close()

	client := New(server.URL, "device", "token", "test", time.Second)
	for attempt := 0; attempt < 2; attempt++ {
		accepted, err := client.DeclareRouteInventory(context.Background(), record)
		if err != nil {
			t.Fatal(err)
		}
		if accepted.ManifestSHA256 != record.ManifestSHA256 || accepted.Generation != 1 {
			t.Fatalf("unexpected acceptance: %#v", accepted)
		}
	}
	var envelope routeInventoryEnvelope
	if err := json.Unmarshal(firstBody, &envelope); err != nil {
		t.Fatal(err)
	}
	if envelope.ManifestSHA256 != record.ManifestSHA256 ||
		envelope.Manifest.RouteName != record.Manifest.RouteName {
		t.Fatalf("unexpected declaration body: %#v", envelope)
	}
}

func TestDeclareRouteInventoryRejectsNonContractAcceptance(t *testing.T) {
	record := apiTestRouteInventory(t)
	tests := []struct {
		name   string
		status int
		body   func() any
	}{
		{
			name:   "asynchronous status",
			status: http.StatusAccepted,
			body: func() any {
				return RouteInventoryAcceptance{
					ManifestSHA256: record.ManifestSHA256,
					Generation:     1,
					State:          "accepted",
				}
			},
		},
		{
			name:   "wrong digest echo",
			status: http.StatusCreated,
			body: func() any {
				return RouteInventoryAcceptance{
					ManifestSHA256: strings.Repeat("f", 64),
					Generation:     1,
					State:          "accepted",
				}
			},
		},
		{
			name:   "unknown response field",
			status: http.StatusCreated,
			body: func() any {
				return map[string]any{
					"manifest_sha256": record.ManifestSHA256,
					"generation":      1,
					"state":           "accepted",
					"unexpected":      true,
				}
			},
		},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, _ *http.Request) {
				response.WriteHeader(test.status)
				_ = json.NewEncoder(response).Encode(test.body())
			}))
			defer server.Close()
			client := New(server.URL, "device", "token", "test", time.Second)
			if _, err := client.DeclareRouteInventory(context.Background(), record); err == nil {
				t.Fatal("invalid route inventory acceptance was accepted")
			}
		})
	}
}

func TestDeclareRouteInventoryRejectsLocalDigestMismatchBeforeNetwork(t *testing.T) {
	called := false
	server := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
		called = true
	}))
	defer server.Close()
	record := apiTestRouteInventory(t)
	record.ManifestSHA256 = strings.Repeat("0", 64)
	client := New(server.URL, "device", "token", "test", time.Second)
	if _, err := client.DeclareRouteInventory(context.Background(), record); err == nil {
		t.Fatal("local manifest digest mismatch was accepted")
	}
	if called {
		t.Fatal("digest-mismatched inventory reached the network")
	}
}

func apiTestRouteInventory(t *testing.T) state.RouteInventory {
	t.Helper()
	mtime := int64(100)
	size := int64(10)
	path := "realdata/route--0/rlog.zst"
	digest := strings.Repeat("a", 64)
	manifest := state.RouteManifest{
		CapabilitySource: "configured+route_union",
		ClosedAt:         time.Date(2026, 7, 29, 12, 0, 0, 0, time.UTC),
		ClosureEvidence:  []string{"no_lock", "offroad", "stable_duration"},
		ExpectedStreams: []state.ExpectedStream{{
			ArtifactType: "rlog",
			Role:         "realdata|rlog|-",
			RootName:     "realdata",
		}},
		Generation:            1,
		MissingSegmentNumbers: []int{},
		RootNames:             []string{"realdata"},
		RouteClosed:           true,
		RouteFiles:            []state.InventoryFile{},
		RouteName:             "route",
		Schema:                inventorycontract.Schema,
		SchemaVersion:         inventorycontract.SchemaVersion,
		Segments: []state.InventorySegment{{
			Files: []state.InventoryFile{{
				ArtifactType: "rlog",
				MTimeNS:      mtime,
				RelativePath: path,
				SHA256:       digest,
				Size:         size,
			}},
			Number: 0,
			Streams: []state.InventoryStream{{
				MTimeNS:      &mtime,
				RelativePath: &path,
				Role:         "realdata|rlog|-",
				SHA256:       &digest,
				Size:         &size,
				Status:       "present",
			}},
		}},
		State: "complete",
	}
	if err := inventorycontract.ValidateAndNormalize(&manifest); err != nil {
		t.Fatal(err)
	}
	_, manifestSHA256, err := inventorycontract.CanonicalManifest(manifest)
	if err != nil {
		t.Fatal(err)
	}
	return state.RouteInventory{
		Manifest:       manifest,
		ManifestSHA256: manifestSHA256,
		CapturedAt:     manifest.ClosedAt,
	}
}

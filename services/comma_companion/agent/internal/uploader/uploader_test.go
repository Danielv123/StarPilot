package uploader

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"starpilot.local/comma-companion-agent/internal/api"
	"starpilot.local/comma-companion-agent/internal/config"
	inventorycontract "starpilot.local/comma-companion-agent/internal/inventory"
	"starpilot.local/comma-companion-agent/internal/journal"
	"starpilot.local/comma-companion-agent/internal/policy"
	"starpilot.local/comma-companion-agent/internal/state"
)

const uploaderTestFileID = "2222222222222222222222222222222222222222222222222222222222222222"

func TestResumableUploadRemovesSpoolOnlyAfterDurableAck(t *testing.T) {
	payload := []byte("complete-rlog")
	digest := sha256.Sum256(payload)
	expectedHash := hex.EncodeToString(digest[:])
	var mu sync.Mutex
	received := make([]byte, 0)
	createCalls := 0
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		if request.Header.Get("Authorization") != "Bearer secret" {
			http.Error(response, "unauthorized", http.StatusUnauthorized)
			return
		}
		switch {
		case request.Method == http.MethodPost && request.URL.Path == "/api/v1/uploads":
			if request.Header.Get("Idempotency-Key") == "" {
				http.Error(response, "missing idempotency", http.StatusBadRequest)
				return
			}
			var declaration api.UploadDeclaration
			if err := json.NewDecoder(request.Body).Decode(&declaration); err != nil {
				http.Error(response, err.Error(), http.StatusBadRequest)
				return
			}
			if declaration.SHA256 != expectedHash {
				http.Error(response, "bad declaration hash", http.StatusBadRequest)
				return
			}
			if declaration.FileID != uploaderTestFileID {
				http.Error(response, "missing stable file selector", http.StatusBadRequest)
				return
			}
			mu.Lock()
			createCalls++
			offset := len(received)
			mu.Unlock()
			writeJSON(response, map[string]any{
				"upload_id": "upload-1",
				"offset":    offset,
				"length":    len(payload),
				"state":     "receiving",
			})
		case request.Method == http.MethodHead && request.URL.Path == "/api/v1/uploads/upload-1":
			mu.Lock()
			offset := len(received)
			mu.Unlock()
			response.Header().Set("Upload-Offset", strconv.Itoa(offset))
			response.Header().Set("Upload-Length", strconv.Itoa(len(payload)))
			if offset == len(payload) {
				response.Header().Set("Upload-State", "durable")
				response.Header().Set("Upload-Durable", "true")
				response.Header().Set("Upload-SHA256", expectedHash)
			} else {
				response.Header().Set("Upload-State", "receiving")
			}
			response.WriteHeader(http.StatusNoContent)
		case request.Method == http.MethodPatch && request.URL.Path == "/api/v1/uploads/upload-1":
			chunk, err := io.ReadAll(request.Body)
			if err != nil {
				http.Error(response, err.Error(), http.StatusBadRequest)
				return
			}
			mu.Lock()
			offset := len(received)
			if request.Header.Get("Upload-Offset") != strconv.Itoa(offset) {
				mu.Unlock()
				http.Error(response, "offset mismatch", http.StatusConflict)
				return
			}
			received = append(received, chunk...)
			next := len(received)
			mu.Unlock()
			durable := next == len(payload)
			stateValue := "receiving"
			hashValue := ""
			if durable {
				stateValue = "durable"
				hashValue = expectedHash
			}
			writeJSON(response, map[string]any{
				"upload_id": "upload-1",
				"offset":    next,
				"length":    len(payload),
				"state":     stateValue,
				"durable":   durable,
				"sha256":    hashValue,
			})
		default:
			http.Error(response, fmt.Sprintf("unexpected %s %s", request.Method, request.URL.Path), http.StatusNotFound)
		}
	}))
	defer server.Close()

	dir := t.TempDir()
	spoolPath := filepath.Join(dir, "spool.data")
	if err := os.WriteFile(spoolPath, payload, 0o600); err != nil {
		t.Fatal(err)
	}
	store, err := journal.Open(filepath.Join(dir, "journal.json"))
	if err != nil {
		t.Fatal(err)
	}
	if err := store.Update(func(data *state.Journal) error {
		data.Files[uploaderTestFileID] = state.File{
			ID:           uploaderTestFileID,
			SpoolPath:    spoolPath,
			RelativePath: "realdata/route--0/rlog",
			ArtifactType: "rlog",
			Size:         int64(len(payload)),
			SHA256:       expectedHash,
			State:        state.FileSpooled,
			SpooledAt:    time.Now().UTC(),
		}
		return nil
	}); err != nil {
		t.Fatal(err)
	}
	client := api.New(server.URL, "device-1", "secret", "test", 5*time.Second)
	subject := New(client, store, policy.New(config.Policy{}), 4, log.New(io.Discard, "", 0))
	for attempts := 0; attempts < 10; attempts++ {
		if store.Snapshot().Files[uploaderTestFileID].State == state.FileDurable {
			break
		}
		didWork, err := subject.ProcessOnce(context.Background())
		if err != nil {
			t.Fatal(err)
		}
		if !didWork {
			t.Fatal("uploader unexpectedly reported no work")
		}
	}
	file := store.Snapshot().Files[uploaderTestFileID]
	if file.State != state.FileDurable || file.UploadOffset != int64(len(payload)) {
		t.Fatalf("upload did not become durable: %#v", file)
	}
	if _, err := os.Stat(spoolPath); !os.IsNotExist(err) {
		t.Fatalf("durable spool was not removed: %v", err)
	}
	mu.Lock()
	defer mu.Unlock()
	if string(received) != string(payload) {
		t.Fatalf("server received %q", string(received))
	}
	if createCalls != 1 {
		t.Fatalf("upload was declared %d times", createCalls)
	}
}

func TestUploadPriorityAndFairPick(t *testing.T) {
	now := time.Now()
	snapshot := state.EmptyJournal()
	add := func(id, artifact, camera string, age time.Duration) {
		snapshot.Files[id] = state.File{
			ID: id, ArtifactType: artifact, Camera: camera, State: state.FileSpooled,
			SpooledAt: now.Add(-age),
		}
	}
	add("road", "video", "road", time.Hour)
	add("rlog", "rlog", "", time.Minute)
	add("qlog", "qlog", "", time.Second)
	prioritized, ok := nextFile(snapshot, now, false)
	if !ok || prioritized.ID != "qlog" {
		t.Fatalf("got prioritized file %#v", prioritized)
	}
	fair, ok := nextFile(snapshot, now, true)
	if !ok || fair.ID != "road" {
		t.Fatalf("fair pick did not choose oldest file: %#v", fair)
	}
}

func TestTerminalUploadIsCanceledBeforeFreshGeneration(t *testing.T) {
	payload := []byte("retry-me")
	var createKey string
	var redeclaredFileID string
	cancelCalls := 0
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		switch {
		case request.Method == http.MethodHead && request.URL.Path == "/api/v1/uploads/old":
			response.Header().Set("Upload-Offset", "4")
			response.Header().Set("Upload-Length", strconv.Itoa(len(payload)))
			response.Header().Set("Upload-State", "failed")
			response.Header().Set("Upload-Terminal", "true")
			response.Header().Set("Upload-Retry-Action", "redeclare")
			response.WriteHeader(http.StatusNoContent)
		case request.Method == http.MethodPost && request.URL.Path == "/api/v1/uploads/old/cancel":
			cancelCalls++
			writeJSON(response, map[string]any{
				"upload_id": "old", "offset": 4, "length": len(payload), "state": "canceled",
			})
		case request.Method == http.MethodPost && request.URL.Path == "/api/v1/uploads":
			createKey = request.Header.Get("Idempotency-Key")
			var declaration api.UploadDeclaration
			if err := json.NewDecoder(request.Body).Decode(&declaration); err != nil {
				http.Error(response, err.Error(), http.StatusBadRequest)
				return
			}
			redeclaredFileID = declaration.FileID
			writeJSON(response, map[string]any{
				"upload_id": "fresh", "offset": 0, "length": len(payload), "state": "receiving",
			})
		case request.Method == http.MethodPatch && request.URL.Path == "/api/v1/uploads/fresh":
			writeJSON(response, map[string]any{
				"upload_id": "fresh", "offset": len(payload), "length": len(payload),
				"state": "receiving",
			})
		default:
			http.Error(response, "unexpected request", http.StatusNotFound)
		}
	}))
	defer server.Close()
	store, spoolPath := uploadStore(t, payload, state.File{
		ID: uploaderTestFileID, UploadID: "old", UploadOffset: 4, UploadAttempt: 0,
		State: state.FileUploading,
	})
	subject := New(
		api.New(server.URL, "device", "token", "test", time.Second),
		store,
		policy.New(config.Policy{}),
		int64(len(payload)),
		log.New(io.Discard, "", 0),
	)

	if didWork, err := subject.ProcessOnce(context.Background()); !didWork || err == nil {
		t.Fatalf("terminal transition didWork=%t err=%v", didWork, err)
	}
	afterTerminal := store.Snapshot()
	file := afterTerminal.Files[uploaderTestFileID]
	if file.State != state.FileCancelPending || file.UploadAttempt != 1 ||
		file.CancelNextState != state.FileRetry {
		t.Fatalf("terminal upload was not durably staged for cleanup: %#v", file)
	}
	if _, ok := afterTerminal.Cancellations[state.CancellationKey(uploaderTestFileID, "old")]; !ok {
		t.Fatal("server cancellation was not queued")
	}
	if _, err := subject.ProcessOnce(context.Background()); err != nil {
		t.Fatal(err)
	}
	file = store.Snapshot().Files[uploaderTestFileID]
	if file.State != state.FileRetry || file.UploadID != "" || file.UploadOffset != 0 {
		t.Fatalf("server cancellation did not unlock retry: %#v", file)
	}
	if _, err := subject.ProcessOnce(context.Background()); err != nil {
		t.Fatal(err)
	}
	if createKey != "upload-create:"+uploaderTestFileID+":1" {
		t.Fatalf("fresh generation key = %q", createKey)
	}
	if redeclaredFileID != uploaderTestFileID {
		t.Fatalf("fresh generation lost stable file selector: %q", redeclaredFileID)
	}
	if cancelCalls != 1 {
		t.Fatalf("cancel calls = %d", cancelCalls)
	}
	if _, err := os.Stat(spoolPath); err != nil {
		t.Fatalf("spool was released before durable acknowledgement: %v", err)
	}
}

func TestAutomaticRedeclarationIsBoundedAndStillCleansTerminalSession(t *testing.T) {
	payload := []byte("bounded")
	cancelCalls := 0
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		switch {
		case request.Method == http.MethodHead:
			response.Header().Set("Upload-Offset", "2")
			response.Header().Set("Upload-Length", strconv.Itoa(len(payload)))
			response.Header().Set("Upload-State", "failed")
			response.Header().Set("Upload-Terminal", "true")
			response.Header().Set("Upload-Retry-Action", "redeclare")
			response.WriteHeader(http.StatusNoContent)
		case request.Method == http.MethodPost && strings.HasSuffix(request.URL.Path, "/cancel"):
			cancelCalls++
			writeJSON(response, map[string]any{
				"upload_id": "second-failed", "offset": 2, "length": len(payload), "state": "canceled",
			})
		default:
			http.Error(response, "unexpected request", http.StatusNotFound)
		}
	}))
	defer server.Close()
	store, _ := uploadStore(t, payload, state.File{
		ID: "file", UploadID: "second-failed", UploadOffset: 2, UploadAttempt: 1,
		AutoRedeclarations: 1, State: state.FileUploading,
	})
	subject := New(
		api.New(server.URL, "device", "token", "test", time.Second),
		store,
		policy.New(config.Policy{}),
		int64(len(payload)),
		log.New(io.Discard, "", 0),
	)
	if _, err := subject.ProcessOnce(context.Background()); !errors.Is(err, errManualRetryRequired) {
		t.Fatalf("expected manual retry requirement, got %v", err)
	}
	file := store.Snapshot().Files["file"]
	if file.State != state.FileCancelPending || file.CancelNextState != state.FileFailed ||
		file.UploadAttempt != 1 {
		t.Fatalf("unexpected bounded state: %#v", file)
	}
	if _, err := subject.ProcessOnce(context.Background()); err != nil {
		t.Fatal(err)
	}
	file = store.Snapshot().Files["file"]
	if file.State != state.FileFailed || file.UploadID != "" {
		t.Fatalf("terminal cleanup did not settle in manual state: %#v", file)
	}
	if cancelCalls != 1 {
		t.Fatalf("cancel calls = %d", cancelCalls)
	}
	if didWork, err := subject.ProcessOnce(context.Background()); didWork || err != nil {
		t.Fatalf("failed upload retried automatically: didWork=%t err=%v", didWork, err)
	}
}

func TestCancelFailurePersistsAcrossJournalReopen(t *testing.T) {
	payload := []byte("cancel")
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		if request.Method == http.MethodPost && strings.HasSuffix(request.URL.Path, "/cancel") {
			http.Error(response, "offline", http.StatusServiceUnavailable)
			return
		}
		if request.Method == http.MethodHead {
			response.Header().Set("Upload-Offset", "1")
			response.Header().Set("Upload-Length", strconv.Itoa(len(payload)))
			response.Header().Set("Upload-State", "receiving")
			response.WriteHeader(http.StatusNoContent)
			return
		}
		http.NotFound(response, request)
	}))
	defer server.Close()
	dir := t.TempDir()
	spoolPath := filepath.Join(dir, "spool.data")
	if err := os.WriteFile(spoolPath, payload, 0o600); err != nil {
		t.Fatal(err)
	}
	journalPath := filepath.Join(dir, "journal.json")
	store, err := journal.Open(journalPath)
	if err != nil {
		t.Fatal(err)
	}
	now := time.Now().UTC()
	if err := store.Update(func(data *state.Journal) error {
		data.Files["file"] = state.File{
			ID: "file", SpoolPath: spoolPath, Size: int64(len(payload)),
			UploadID: "session", State: state.FileCancelPending,
			CancelNextState: state.FileCanceled, CancelRequestedAt: now,
		}
		data.Cancellations[state.CancellationKey("file", "session")] = state.UploadCancellation{
			FileID: "file", UploadID: "session", RequestedAt: now,
		}
		return nil
	}); err != nil {
		t.Fatal(err)
	}
	subject := New(
		api.New(server.URL, "device", "token", "test", time.Second),
		store,
		policy.New(config.Policy{}),
		int64(len(payload)),
		log.New(io.Discard, "", 0),
	)
	if _, err := subject.ProcessOnce(context.Background()); err == nil {
		t.Fatal("cancel failure was not returned")
	}
	reopened, err := journal.Open(journalPath)
	if err != nil {
		t.Fatal(err)
	}
	snapshot := reopened.Snapshot()
	cancellation := snapshot.Cancellations[state.CancellationKey("file", "session")]
	if cancellation.Attempts != 1 || cancellation.NextAttemptAt.IsZero() ||
		snapshot.Files["file"].State != state.FileCancelPending {
		t.Fatalf("cancel retry was not durable: %#v %#v", cancellation, snapshot.Files["file"])
	}
}

func TestMissingServerSessionAdvancesGeneration(t *testing.T) {
	payload := []byte("missing")
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		http.NotFound(response, request)
	}))
	defer server.Close()
	store, _ := uploadStore(t, payload, state.File{
		ID: "file", UploadID: "gone", UploadAttempt: 4, State: state.FileUploading,
	})
	subject := New(
		api.New(server.URL, "device", "token", "test", time.Second),
		store,
		policy.New(config.Policy{}),
		int64(len(payload)),
		log.New(io.Discard, "", 0),
	)
	if _, err := subject.ProcessOnce(context.Background()); err == nil {
		t.Fatal("missing session transition did not return")
	}
	file := store.Snapshot().Files["file"]
	if file.UploadAttempt != 5 || file.UploadID != "" || file.State != state.FileRetry {
		t.Fatalf("generation did not advance after 404: %#v", file)
	}
}

func TestFullReceivingSessionNeverRecreates(t *testing.T) {
	payload := []byte("full")
	createCalls := 0
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		if request.Method == http.MethodPost && request.URL.Path == "/api/v1/uploads" {
			createCalls++
			http.Error(response, "must not recreate", http.StatusInternalServerError)
			return
		}
		response.Header().Set("Upload-Offset", strconv.Itoa(len(payload)))
		response.Header().Set("Upload-Length", strconv.Itoa(len(payload)))
		response.Header().Set("Upload-State", "receiving")
		response.WriteHeader(http.StatusNoContent)
	}))
	defer server.Close()
	store, _ := uploadStore(t, payload, state.File{
		ID: "file", UploadID: "full-session", UploadOffset: int64(len(payload)),
		State: state.FileUploading,
	})
	subject := New(
		api.New(server.URL, "device", "token", "test", time.Second),
		store,
		policy.New(config.Policy{}),
		int64(len(payload)),
		log.New(io.Discard, "", 0),
	)
	if _, err := subject.ProcessOnce(context.Background()); err == nil {
		t.Fatal("non-durable full session was accepted")
	}
	file := store.Snapshot().Files["file"]
	if file.UploadID != "full-session" || file.UploadAttempt != 0 {
		t.Fatalf("full receiving session was rolled over: %#v", file)
	}
	if createCalls != 0 {
		t.Fatalf("create calls = %d", createCalls)
	}
}

func TestDurableAckRequiresExactLengthAndServerHashBeforeUnlink(t *testing.T) {
	payload := []byte("integrity")
	digest := sha256.Sum256(payload)
	expectedHash := hex.EncodeToString(digest[:])
	for _, test := range []struct {
		name   string
		length int
		hash   string
	}{
		{name: "missing-hash", length: len(payload), hash: ""},
		{name: "missing-length", length: 0, hash: expectedHash},
		{name: "wrong-hash", length: len(payload), hash: strings.Repeat("0", 64)},
	} {
		t.Run(test.name, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
				response.Header().Set("Upload-Offset", strconv.Itoa(len(payload)))
				response.Header().Set("Upload-Length", strconv.Itoa(test.length))
				response.Header().Set("Upload-State", "complete")
				response.Header().Set("Upload-Durable", "true")
				if test.hash != "" {
					response.Header().Set("Upload-SHA256", test.hash)
				}
				response.WriteHeader(http.StatusNoContent)
			}))
			defer server.Close()
			store, spoolPath := uploadStore(t, payload, state.File{
				ID: "file", UploadID: "session", UploadOffset: int64(len(payload)),
				State: state.FileUploading,
			})
			subject := New(
				api.New(server.URL, "device", "token", "test", time.Second),
				store,
				policy.New(config.Policy{}),
				int64(len(payload)),
				log.New(io.Discard, "", 0),
			)
			if _, err := subject.ProcessOnce(context.Background()); err == nil {
				t.Fatal("incomplete durable acknowledgement was accepted")
			}
			if store.Snapshot().Files["file"].State == state.FileDurable {
				t.Fatal("file became durable without complete integrity acknowledgement")
			}
			if _, err := os.Stat(spoolPath); err != nil {
				t.Fatalf("spool was unlinked without complete integrity acknowledgement: %v", err)
			}
		})
	}
}

func TestRouteInventoryDeclarationBypassesContentPolicyAndPause(t *testing.T) {
	record := uploaderTestRouteInventory(t, "route", 1, nil)
	var declarationKey string
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		if request.Method != http.MethodPost || request.URL.Path != "/api/v1/route-inventories" {
			http.NotFound(response, request)
			return
		}
		declarationKey = request.Header.Get("Idempotency-Key")
		response.WriteHeader(http.StatusCreated)
		writeJSON(response, api.RouteInventoryAcceptance{
			ManifestSHA256: record.ManifestSHA256,
			Generation:     1,
			State:          "accepted",
		})
	}))
	defer server.Close()
	store, err := journal.Open(filepath.Join(t.TempDir(), "journal.json"))
	if err != nil {
		t.Fatal(err)
	}
	if err := store.Update(func(data *state.Journal) error {
		data.Paused = true
		data.Inventories[record.ManifestSHA256] = record
		return nil
	}); err != nil {
		t.Fatal(err)
	}
	policyReader := policy.New(config.Policy{
		UploadOnlyOffroad: true,
		RequireWiFi:       true,
		WiFiInterface:     "definitely-missing",
	})
	subject := New(
		api.New(server.URL, "device", "secret", "test", time.Second),
		store,
		policyReader,
		1024,
		log.New(io.Discard, "", 0),
	)
	didWork, err := subject.ProcessOnce(context.Background())
	if err != nil || !didWork {
		t.Fatalf("inventory declaration was blocked by content policy: work=%t err=%v", didWork, err)
	}
	declared := store.Snapshot().Inventories[record.ManifestSHA256]
	if declared.DeclaredAt.IsZero() || declared.Attempts != 0 ||
		store.Snapshot().Counters.InventoriesDeclared != 1 {
		t.Fatalf("acceptance was not persisted: %#v", declared)
	}
	if declarationKey != "route-inventory:"+record.ManifestSHA256 {
		t.Fatalf("unexpected inventory idempotency key %q", declarationKey)
	}
}

func TestRouteInventoryRetryPersistsAndBlocksSupersedingGeneration(t *testing.T) {
	first := uploaderTestRouteInventory(t, "route", 1, nil)
	second := uploaderTestRouteInventory(t, "route", 2, &first.ManifestSHA256)
	now := time.Date(2026, 7, 29, 12, 0, 0, 0, time.UTC)
	requests := make([]int, 0)
	failFirst := true
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		var envelope struct {
			ManifestSHA256 string              `json:"manifest_sha256"`
			Manifest       state.RouteManifest `json:"manifest"`
		}
		if err := json.NewDecoder(request.Body).Decode(&envelope); err != nil {
			http.Error(response, err.Error(), http.StatusBadRequest)
			return
		}
		requests = append(requests, envelope.Manifest.Generation)
		if failFirst {
			failFirst = false
			http.Error(response, "temporary", http.StatusInternalServerError)
			return
		}
		response.WriteHeader(http.StatusOK)
		writeJSON(response, api.RouteInventoryAcceptance{
			ManifestSHA256: envelope.ManifestSHA256,
			Generation:     envelope.Manifest.Generation,
			State:          "accepted",
		})
	}))
	defer server.Close()
	journalPath := filepath.Join(t.TempDir(), "journal.json")
	store, err := journal.Open(journalPath)
	if err != nil {
		t.Fatal(err)
	}
	if err := store.Update(func(data *state.Journal) error {
		data.Inventories[first.ManifestSHA256] = first
		data.Inventories[second.ManifestSHA256] = second
		return nil
	}); err != nil {
		t.Fatal(err)
	}
	subject := New(
		api.New(server.URL, "device", "secret", "test", time.Second),
		store,
		policy.New(config.Policy{}),
		1024,
		log.New(io.Discard, "", 0),
	)
	subject.now = func() time.Time { return now }
	if didWork, err := subject.ProcessOnce(context.Background()); err == nil || !didWork {
		t.Fatalf("first declaration failure was not surfaced: work=%t err=%v", didWork, err)
	}
	failed := store.Snapshot().Inventories[first.ManifestSHA256]
	if failed.Attempts != 1 || failed.NextAttemptAt != now.Add(5*time.Second) ||
		store.Snapshot().Counters.InventoryErrors != 1 {
		t.Fatalf("inventory retry was not persisted: %#v", failed)
	}
	reopened, err := journal.Open(journalPath)
	if err != nil {
		t.Fatal(err)
	}
	subject.journal = reopened
	now = now.Add(time.Second)
	if didWork, err := subject.ProcessOnce(context.Background()); err != nil || didWork {
		t.Fatalf("superseding generation bypassed predecessor backoff: work=%t err=%v", didWork, err)
	}
	now = now.Add(5 * time.Second)
	if didWork, err := subject.ProcessOnce(context.Background()); err != nil || !didWork {
		t.Fatalf("predecessor retry did not succeed: work=%t err=%v", didWork, err)
	}
	if didWork, err := subject.ProcessOnce(context.Background()); err != nil || !didWork {
		t.Fatalf("superseding generation did not follow predecessor: work=%t err=%v", didWork, err)
	}
	if !reflect.DeepEqual(requests, []int{1, 1, 2}) {
		t.Fatalf("inventory generations were declared out of order: %#v", requests)
	}
	snapshot := reopened.Snapshot()
	if snapshot.Inventories[first.ManifestSHA256].DeclaredAt.IsZero() ||
		snapshot.Inventories[second.ManifestSHA256].DeclaredAt.IsZero() {
		t.Fatal("accepted inventory chain was not persisted")
	}
}

func uploaderTestRouteInventory(
	t *testing.T,
	route string,
	generation int,
	previous *string,
) state.RouteInventory {
	t.Helper()
	mtime := int64(100)
	size := int64(10)
	relativePath := "realdata/" + route + "--0/rlog.zst"
	digest := strings.Repeat("a", 64)
	manifest := state.RouteManifest{
		CapabilitySource: "configured+route_union",
		ClosedAt:         time.Date(2026, 7, 29, 12, 0, generation, 0, time.UTC),
		ClosureEvidence:  []string{"no_lock", "offroad", "stable_duration"},
		ExpectedStreams: []state.ExpectedStream{{
			ArtifactType: "rlog",
			Role:         "realdata|rlog|-",
			RootName:     "realdata",
		}},
		Generation:             generation,
		MissingSegmentNumbers:  []int{},
		PreviousManifestSHA256: previous,
		RootNames:              []string{"realdata"},
		RouteClosed:            true,
		RouteFiles:             []state.InventoryFile{},
		RouteName:              route,
		Schema:                 inventorycontract.Schema,
		SchemaVersion:          inventorycontract.SchemaVersion,
		Segments: []state.InventorySegment{{
			Files: []state.InventoryFile{{
				ArtifactType: "rlog",
				MTimeNS:      mtime,
				RelativePath: relativePath,
				SHA256:       digest,
				Size:         size,
			}},
			Number: 0,
			Streams: []state.InventoryStream{{
				MTimeNS:      &mtime,
				RelativePath: &relativePath,
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
	contentSHA256, err := inventorycontract.ContentSHA256(manifest)
	if err != nil {
		t.Fatal(err)
	}
	return state.RouteInventory{
		ContentSHA256:  contentSHA256,
		Manifest:       manifest,
		ManifestSHA256: manifestSHA256,
		CapturedAt:     manifest.ClosedAt,
	}
}

func uploadStore(t *testing.T, payload []byte, file state.File) (*journal.Store, string) {
	t.Helper()
	dir := t.TempDir()
	spoolPath := filepath.Join(dir, "spool.data")
	if err := os.WriteFile(spoolPath, payload, 0o600); err != nil {
		t.Fatal(err)
	}
	info, err := os.Stat(spoolPath)
	if err != nil {
		t.Fatal(err)
	}
	digest := sha256.Sum256(payload)
	file.SpoolPath = spoolPath
	file.Size = int64(len(payload))
	file.ModTimeNS = info.ModTime().UnixNano()
	file.SHA256 = hex.EncodeToString(digest[:])
	file.RelativePath = "realdata/route--0/rlog"
	file.ArtifactType = "rlog"
	file.SpooledAt = time.Now().UTC()
	store, err := journal.Open(filepath.Join(dir, "journal.json"))
	if err != nil {
		t.Fatal(err)
	}
	if err := store.Update(func(data *state.Journal) error {
		data.Files[file.ID] = file
		return nil
	}); err != nil {
		t.Fatal(err)
	}
	return store, spoolPath
}

func writeJSON(response http.ResponseWriter, value any) {
	response.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(response).Encode(value)
}

func TestChunkChecksumUsesExpectedWireFormat(t *testing.T) {
	var checksum string
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		checksum = request.Header.Get("Upload-Checksum")
		writeJSON(response, map[string]any{"upload_id": "id", "offset": 3, "length": 3})
	}))
	defer server.Close()
	client := api.New(server.URL, "device", "token", "test", time.Second)
	if _, err := client.PatchUpload(context.Background(), "id", 0, 3, []byte("abc")); err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(checksum, "sha256 ") {
		t.Fatalf("unexpected checksum header %q", checksum)
	}
}

package commands

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sync/atomic"
	"testing"
	"time"

	"starpilot.local/comma-companion-agent/internal/api"
	"starpilot.local/comma-companion-agent/internal/config"
	"starpilot.local/comma-companion-agent/internal/journal"
	"starpilot.local/comma-companion-agent/internal/policy"
	"starpilot.local/comma-companion-agent/internal/state"
)

type quietLogger struct{}

func (quietLogger) Printf(string, ...any) {}

func TestTypedCommandAllowlistAndCancelRetainsSpool(t *testing.T) {
	dir := t.TempDir()
	spoolPath := filepath.Join(dir, "file.data")
	if err := os.WriteFile(spoolPath, []byte("data"), 0o600); err != nil {
		t.Fatal(err)
	}
	store, err := journal.Open(filepath.Join(dir, "journal.json"))
	if err != nil {
		t.Fatal(err)
	}
	if err := store.Update(func(data *state.Journal) error {
		data.Files["file"] = state.File{
			ID: "file", SpoolPath: spoolPath, State: state.FileSpooled, Size: 4,
		}
		return nil
	}); err != nil {
		t.Fatal(err)
	}
	rescan := make(chan struct{}, 1)
	handler := New(config.Commands{}, nil, store, policy.New(config.Policy{}), rescan, quietLogger{})

	if _, commandError, _ := handler.execute(api.Command{Type: "run_shell", Args: map[string]any{"command": "true"}}); commandError == "" {
		t.Fatal("unknown command type was accepted")
	}
	if _, commandError, _ := handler.execute(api.Command{
		Type: "cancel_upload",
		Args: map[string]any{"file_id": "file"},
	}); commandError != "" {
		t.Fatal(commandError)
	}
	if store.Snapshot().Files["file"].State != state.FileCanceled {
		t.Fatal("cancel did not persist")
	}
	if _, err := os.Stat(spoolPath); err != nil {
		t.Fatalf("cancel removed protected data: %v", err)
	}
	if _, commandError, _ := handler.execute(api.Command{Type: "rescan"}); commandError != "" {
		t.Fatal(commandError)
	}
	select {
	case <-rescan:
	default:
		t.Fatal("rescan command did not signal scanner")
	}
}

func TestCancelAndRetryPersistServerCleanupBeforeSessionReset(t *testing.T) {
	dir := t.TempDir()
	spoolPath := filepath.Join(dir, "file.data")
	if err := os.WriteFile(spoolPath, []byte("data"), 0o600); err != nil {
		t.Fatal(err)
	}
	store, err := journal.Open(filepath.Join(dir, "journal.json"))
	if err != nil {
		t.Fatal(err)
	}
	if err := store.Update(func(data *state.Journal) error {
		data.Files["file"] = state.File{
			ID: "file", SourcePath: spoolPath, SpoolPath: spoolPath,
			UploadID: "session", State: state.FileUploading, Size: 4,
		}
		return nil
	}); err != nil {
		t.Fatal(err)
	}
	handler := New(
		config.Commands{},
		nil,
		store,
		policy.New(config.Policy{}),
		make(chan struct{}, 1),
		quietLogger{},
	)
	if _, commandError, _ := handler.execute(api.Command{
		Type: "cancel_upload", Args: map[string]any{"file_id": "file"},
	}); commandError != "" {
		t.Fatal(commandError)
	}
	snapshot := store.Snapshot()
	file := snapshot.Files["file"]
	if file.State != state.FileCancelPending || file.CancelNextState != state.FileCanceled ||
		file.UploadID != "session" {
		t.Fatalf("cancel was not staged durably: %#v", file)
	}
	if _, ok := snapshot.Cancellations[state.CancellationKey("file", "session")]; !ok {
		t.Fatal("server cancellation missing")
	}

	if _, commandError, _ := handler.execute(api.Command{
		Type: "retry_upload", Args: map[string]any{"file_id": "file"},
	}); commandError != "" {
		t.Fatal(commandError)
	}
	snapshot = store.Snapshot()
	file = snapshot.Files["file"]
	if file.State != state.FileCancelPending || file.CancelNextState != state.FileRetry ||
		file.UploadAttempt != 1 || file.UploadID != "session" {
		t.Fatalf("retry reused or cleared a live server session: %#v", file)
	}
}

func TestUploadSelectorsAreConjunctiveAndAllIsExclusive(t *testing.T) {
	dir := t.TempDir()
	store, err := journal.Open(filepath.Join(dir, "journal.json"))
	if err != nil {
		t.Fatal(err)
	}
	if err := store.Update(func(data *state.Journal) error {
		data.Files["file-a"] = state.File{
			ID: "file-a", UploadID: "upload-a", State: state.FileUploading,
			SpoolPath: filepath.Join(dir, "a"), Size: 1,
		}
		data.Files["file-b"] = state.File{
			ID: "file-b", UploadID: "upload-b", State: state.FileUploading,
			SpoolPath: filepath.Join(dir, "b"), Size: 1,
		}
		return nil
	}); err != nil {
		t.Fatal(err)
	}
	handler := New(
		config.Commands{},
		nil,
		store,
		policy.New(config.Policy{}),
		make(chan struct{}, 1),
		quietLogger{},
	)
	if _, commandError, _ := handler.execute(api.Command{
		Type: "cancel_upload",
		Args: map[string]any{"file_id": "file-a", "upload_id": "upload-b"},
	}); commandError == "" {
		t.Fatal("mismatched file_id and upload_id selected multiple records")
	}
	snapshot := store.Snapshot()
	if snapshot.Files["file-a"].State != state.FileUploading ||
		snapshot.Files["file-b"].State != state.FileUploading {
		t.Fatalf("conjunctive selector changed a mismatched file: %#v", snapshot.Files)
	}
	if err := validateArgs("cancel_upload", map[string]any{
		"scope": "all", "file_id": "file-a",
	}); err == nil {
		t.Fatal("scope=all was accepted with a narrower selector")
	}
}

func TestSourceOnlyReleasedRetryPreservesGenerationForRespool(t *testing.T) {
	dir := t.TempDir()
	source := filepath.Join(dir, "source.data")
	if err := os.WriteFile(source, []byte("data"), 0o600); err != nil {
		t.Fatal(err)
	}
	info, err := os.Stat(source)
	if err != nil {
		t.Fatal(err)
	}
	store, err := journal.Open(filepath.Join(dir, "journal.json"))
	if err != nil {
		t.Fatal(err)
	}
	if err := store.Update(func(data *state.Journal) error {
		data.Files["file"] = state.File{
			ID: "file", SourcePath: source, SpoolPath: filepath.Join(dir, "missing.data"),
			Size: 4, ModTimeNS: info.ModTime().UnixNano(), State: state.FileReleased,
			UploadAttempt: 2,
		}
		return nil
	}); err != nil {
		t.Fatal(err)
	}
	handler := New(
		config.Commands{},
		nil,
		store,
		policy.New(config.Policy{}),
		make(chan struct{}, 1),
		quietLogger{},
	)
	if _, commandError, _ := handler.execute(api.Command{
		Type: "retry_upload", Args: map[string]any{"file_id": "file"},
	}); commandError != "" {
		t.Fatal(commandError)
	}
	file := store.Snapshot().Files["file"]
	if file.State != state.FileReleased || !file.NeedsRespool || file.UploadAttempt != 3 {
		t.Fatalf("source-only retry was not queued for respool: %#v", file)
	}
}

func TestRetryByDeclaredFileIDSurvivesTerminalSessionCleanup(t *testing.T) {
	const fileID = "3333333333333333333333333333333333333333333333333333333333333333"
	dir := t.TempDir()
	spoolPath := filepath.Join(dir, "retained.data")
	if err := os.WriteFile(spoolPath, []byte("data"), 0o600); err != nil {
		t.Fatal(err)
	}
	store, err := journal.Open(filepath.Join(dir, "journal.json"))
	if err != nil {
		t.Fatal(err)
	}
	if err := store.Update(func(data *state.Journal) error {
		data.Files[fileID] = state.File{
			ID:            fileID,
			SpoolPath:     spoolPath,
			Size:          4,
			State:         state.FileFailed,
			UploadID:      "",
			UploadAttempt: 2,
		}
		return nil
	}); err != nil {
		t.Fatal(err)
	}
	handler := New(
		config.Commands{},
		nil,
		store,
		policy.New(config.Policy{}),
		make(chan struct{}, 1),
		quietLogger{},
	)
	if _, commandError, _ := handler.execute(api.Command{
		Type: "retry_upload",
		Args: map[string]any{"file_id": fileID},
	}); commandError != "" {
		t.Fatal(commandError)
	}
	file := store.Snapshot().Files[fileID]
	if file.State != state.FileRetry || file.UploadAttempt != 3 ||
		file.UploadID != "" || file.NeedsRespool {
		t.Fatalf("stable file selector did not revive retained terminal data: %#v", file)
	}
}

func TestActionReportsRunningThenActualFailure(t *testing.T) {
	dir := t.TempDir()
	store, err := journal.Open(filepath.Join(dir, "journal.json"))
	if err != nil {
		t.Fatal(err)
	}
	results := make(chan api.CommandResult, 4)
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		var result api.CommandResult
		if err := json.NewDecoder(request.Body).Decode(&result); err != nil {
			t.Error(err)
		}
		results <- result
		response.WriteHeader(http.StatusNoContent)
	}))
	defer server.Close()
	client := api.New(server.URL, "device", "token", "test", time.Second)
	handler := New(
		config.Commands{AllowAgentRestart: true},
		client,
		store,
		policy.New(config.Policy{}),
		make(chan struct{}, 1),
		quietLogger{},
	)
	now := time.Now().UTC()
	request := handler.Handle(context.Background(), api.Command{
		ID: "0123456789abcdef0123456789abcdef", DeviceID: "device",
		Type: string(ActionRestart), State: "delivered",
		IssuedAt: now, ExpiresAt: now.Add(time.Minute), Args: map[string]any{},
	})
	if request.Action != ActionRestart {
		t.Fatalf("restart action was not queued: %#v", request)
	}
	first := <-results
	if first.State != "running" {
		t.Fatalf("pre-invocation result was not running: %#v", first)
	}
	if store.Snapshot().Commands[request.CommandID].State != "running" {
		t.Fatal("journal claimed terminal success before invocation")
	}
	invocationError := errors.New("restart invocation failed")
	handler.CompleteAction(context.Background(), request.CommandID, invocationError)
	second := <-results
	if second.State != "failed" || second.Error != invocationError.Error() {
		t.Fatalf("actual failure was not reported: %#v", second)
	}
}

func TestActionWaitsForRunningReportAfterNetworkFailure(t *testing.T) {
	dir := t.TempDir()
	store, err := journal.Open(filepath.Join(dir, "journal.json"))
	if err != nil {
		t.Fatal(err)
	}
	var networkAvailable atomic.Bool
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		if !networkAvailable.Load() {
			http.Error(response, "offline", http.StatusServiceUnavailable)
			return
		}
		response.WriteHeader(http.StatusNoContent)
	}))
	defer server.Close()
	handler := New(
		config.Commands{AllowAgentRestart: true},
		api.New(server.URL, "device", "token", "test", time.Second),
		store,
		policy.New(config.Policy{}),
		make(chan struct{}, 1),
		quietLogger{},
	)
	now := time.Now().UTC()
	request := handler.Handle(context.Background(), api.Command{
		ID: "fedcba9876543210fedcba9876543210", DeviceID: "device",
		Type: string(ActionRestart), State: "delivered",
		IssuedAt: now, ExpiresAt: now.Add(time.Minute), Args: map[string]any{},
	})
	if request.Action != ActionNone {
		t.Fatalf("action escaped before running acknowledgement: %#v", request)
	}
	record := store.Snapshot().Commands["fedcba9876543210fedcba9876543210"]
	if record.State != "running" || record.Reported {
		t.Fatalf("network failure was not retained for reporting: %#v", record)
	}
	networkAvailable.Store(true)
	request = handler.FlushPending(context.Background())
	if request.Action != ActionRestart {
		t.Fatalf("action was not released after running acknowledgement: %#v", request)
	}
}

func TestPrivilegedActionsFailClosedWithoutOffroadEvidence(t *testing.T) {
	handler := &Handler{
		config: config.Commands{AllowStarPilotRestart: true, AllowPowerCommands: true},
		policy: policy.New(config.Policy{}),
	}
	for _, commandType := range []string{
		string(ActionRestartStarPilot),
		string(ActionReboot),
		string(ActionShutdown),
	} {
		_, commandError, action := handler.execute(api.Command{
			Type: commandType, Args: map[string]any{"reason": "test"},
		})
		if commandError == "" || action != ActionNone {
			t.Fatalf("%s was available: error=%q action=%q", commandType, commandError, action)
		}
	}
}

func TestPrivilegedActionsQueueOnlyAfterElapsedOffroadGate(t *testing.T) {
	for _, test := range []struct {
		command api.Command
		action  Action
		reason  string
	}{
		{
			command: api.Command{Type: string(ActionRestartStarPilot), Args: map[string]any{}},
			action:  ActionRestartStarPilot,
			reason:  "remote StarPilot restart",
		},
		{
			command: api.Command{
				Type: string(ActionReboot),
				Args: map[string]any{"reason": "owner requested reboot"},
			},
			action: ActionReboot,
			reason: "owner requested reboot",
		},
		{
			command: api.Command{
				Type: string(ActionShutdown),
				Args: map[string]any{"reason": "owner requested shutdown"},
			},
			action: ActionShutdown,
			reason: "owner requested shutdown",
		},
	} {
		t.Run(test.command.Type, func(t *testing.T) {
			dir := t.TempDir()
			offroad := filepath.Join(dir, "IsOffroad")
			onroad := filepath.Join(dir, "IsOnroad")
			if err := os.WriteFile(offroad, []byte("1"), 0o600); err != nil {
				t.Fatal(err)
			}
			if err := os.WriteFile(onroad, []byte("0"), 0o600); err != nil {
				t.Fatal(err)
			}
			reader := policy.New(config.Policy{
				OffroadStateFile:      offroad,
				OnroadStateFile:       onroad,
				OffroadStableDuration: config.Duration{Duration: time.Millisecond},
				OffroadMaxAge:         config.Duration{Duration: time.Hour},
				TrueValues:            []string{"1"},
			})
			reader.Read()
			time.Sleep(2 * time.Millisecond)
			handler := &Handler{
				config: config.Commands{
					AllowStarPilotRestart: true,
					AllowPowerCommands:    true,
				},
				policy: reader,
			}
			_, commandError, action := handler.execute(test.command)
			if commandError != "" || action != test.action {
				t.Fatalf(
					"elapsed offroad action was not queued: error=%q action=%q",
					commandError,
					action,
				)
			}
			if reason := actionReason(test.command, action); reason != test.reason {
				t.Fatalf("wrong durable helper reason: %q", reason)
			}
		})
	}
}

func TestRestartAgentCompletesOnlyAfterCommittedExitAndNextStartup(t *testing.T) {
	dir := t.TempDir()
	store, err := journal.Open(filepath.Join(dir, "journal.json"))
	if err != nil {
		t.Fatal(err)
	}
	now := time.Now().UTC()
	if err := store.Update(func(data *state.Journal) error {
		data.Commands["committed"] = state.CommandRecord{
			ID: "committed", Type: string(ActionRestart), State: "running",
			Action: string(ActionRestart), StartedAt: now,
			ActionQueuedAt: now, ActionInvokedAt: now,
		}
		data.Commands["not-invoked"] = state.CommandRecord{
			ID: "not-invoked", Type: string(ActionRestart), State: "running",
			Action: string(ActionRestart), StartedAt: now, ActionQueuedAt: now,
		}
		return nil
	}); err != nil {
		t.Fatal(err)
	}
	handler := New(
		config.Commands{},
		nil,
		store,
		policy.New(config.Policy{}),
		make(chan struct{}, 1),
		quietLogger{},
	)
	if err := handler.RecoverOnStartup(); err != nil {
		t.Fatal(err)
	}
	snapshot := store.Snapshot()
	if snapshot.Commands["committed"].State != "succeeded" {
		t.Fatalf("committed restart did not complete on startup: %#v", snapshot.Commands["committed"])
	}
	if snapshot.Commands["not-invoked"].State != "failed" {
		t.Fatalf("uninvoked restart was falsely successful: %#v", snapshot.Commands["not-invoked"])
	}
}

func TestPrivilegedActionRecoveryRequeuesSameCommandAndReason(t *testing.T) {
	dir := t.TempDir()
	store, err := journal.Open(filepath.Join(dir, "journal.json"))
	if err != nil {
		t.Fatal(err)
	}
	now := time.Now().UTC()
	const commandID = "0123456789abcdef0123456789abcdef"
	if err := store.Update(func(data *state.Journal) error {
		data.Commands[commandID] = state.CommandRecord{
			ID:              commandID,
			Type:            string(ActionReboot),
			State:           "running",
			Reported:        true,
			Action:          string(ActionReboot),
			ActionReason:    "owner requested reboot",
			StartedAt:       now,
			ActionQueuedAt:  now,
			ActionInvokedAt: now,
		}
		return nil
	}); err != nil {
		t.Fatal(err)
	}
	handler := New(
		config.Commands{},
		nil,
		store,
		policy.New(config.Policy{}),
		make(chan struct{}, 1),
		quietLogger{},
	)
	if err := handler.RecoverOnStartup(); err != nil {
		t.Fatal(err)
	}
	request := handler.FlushPending(context.Background())
	if request.CommandID != commandID || request.Action != ActionReboot ||
		request.Reason != "owner requested reboot" {
		t.Fatalf("privileged action was not safely requeued: %#v", request)
	}
}

func TestStarPilotRestartRequiresElapsedOffroadState(t *testing.T) {
	dir := t.TempDir()
	offroad := filepath.Join(dir, "IsOffroad")
	onroad := filepath.Join(dir, "IsOnroad")
	if err := os.WriteFile(offroad, []byte("1"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(onroad, []byte("0"), 0o600); err != nil {
		t.Fatal(err)
	}
	store, err := journal.Open(filepath.Join(dir, "journal.json"))
	if err != nil {
		t.Fatal(err)
	}
	policyReader := policy.New(config.Policy{
		OffroadStateFile:      offroad,
		OnroadStateFile:       onroad,
		OffroadStableDuration: config.Duration{Duration: time.Hour},
		OffroadMaxAge:         config.Duration{Duration: time.Hour},
		TrueValues:            []string{"1"},
	})
	handler := New(
		config.Commands{AllowStarPilotRestart: true},
		nil,
		store,
		policyReader,
		make(chan struct{}, 1),
		quietLogger{},
	)
	if _, commandError, action := handler.execute(api.Command{Type: "restart_starpilot"}); commandError == "" || action != ActionNone {
		t.Fatal("rapid offroad reads permitted StarPilot restart")
	}
}

func TestRequiresOffroadCommandStillReportsUnsupportedTypeAsRejected(t *testing.T) {
	dir := t.TempDir()
	offroad := filepath.Join(dir, "IsOffroad")
	if err := os.WriteFile(offroad, []byte("1"), 0o600); err != nil {
		t.Fatal(err)
	}
	store, err := journal.Open(filepath.Join(dir, "journal.json"))
	if err != nil {
		t.Fatal(err)
	}
	policyReader := policy.New(config.Policy{
		OffroadStateFile: offroad,
		OffroadMaxAge:    config.Duration{Duration: time.Hour},
		TrueValues:       []string{"1"},
	})
	policyReader.Read()
	var result api.CommandResult
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		if err := json.NewDecoder(request.Body).Decode(&result); err != nil {
			t.Error(err)
		}
		response.WriteHeader(http.StatusNoContent)
	}))
	defer server.Close()
	client := api.New(server.URL, "device", "token", "test", time.Second)
	handler := New(config.Commands{}, client, store, policyReader, make(chan struct{}, 1), quietLogger{})
	now := time.Now().UTC()
	handler.Handle(context.Background(), api.Command{
		ID:              "0123456789abcdef0123456789abcdef",
		Type:            "run_shell",
		IssuedAt:        now,
		ExpiresAt:       now.Add(time.Minute),
		RequiresOffroad: true,
	})
	if result.State != "rejected" || result.Error != "unsupported command type" {
		t.Fatalf("unexpected command result: %#v", result)
	}
}

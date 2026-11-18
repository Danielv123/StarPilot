package storageguard

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"starpilot.local/comma-companion-agent/internal/config"
	"starpilot.local/comma-companion-agent/internal/journal"
	"starpilot.local/comma-companion-agent/internal/state"
)

type discardLogger struct{}

func (discardLogger) Printf(string, ...any) {}

func TestRetentionLimitReleasesOldestSpoolLink(t *testing.T) {
	dir := t.TempDir()
	store, err := journal.Open(filepath.Join(dir, "journal.json"))
	if err != nil {
		t.Fatal(err)
	}
	oldSource, oldPath, oldInfo := writeSourceAndSpoolLink(t, dir, "old", 8)
	newSource, newPath, newInfo := writeSourceAndSpoolLink(t, dir, "new", 8)
	if err := store.Update(func(data *state.Journal) error {
		data.Files["old"] = state.File{
			ID: "old", SourcePath: oldSource, SpoolPath: oldPath,
			Size: 8, ModTimeNS: oldInfo.ModTime().UnixNano(), State: state.FileSpooled,
			SpooledAt: time.Unix(1, 0),
		}
		data.Files["new"] = state.File{
			ID: "new", SourcePath: newSource, SpoolPath: newPath,
			Size: 8, ModTimeNS: newInfo.ModTime().UnixNano(), State: state.FileSpooled,
			SpooledAt: time.Unix(2, 0),
		}
		return nil
	}); err != nil {
		t.Fatal(err)
	}
	guard := New(config.Storage{
		MaxRetainedBytes:  8,
		MinFreeBytes:      1,
		EmergencyBehavior: "release_oldest",
	}, dir, store, discardLogger{})
	status := guard.Enforce()
	if status.ReleasedFiles != 1 || status.RetainedBytes != 8 {
		t.Fatalf("unexpected guard status: %#v", status)
	}
	if _, err := os.Stat(oldPath); !os.IsNotExist(err) {
		t.Fatalf("oldest path still exists: %v", err)
	}
	released := store.Snapshot().Files["old"]
	if released.State != state.FileReleased || !released.NeedsRespool {
		t.Fatalf("journal did not preserve retryable emergency release: %#v", released)
	}
	if !strings.Contains(released.LastError, "exact unchanged source retained for retry") {
		t.Fatalf("release did not explain retry preservation: %q", released.LastError)
	}
	if _, err := os.Stat(oldSource); err != nil {
		t.Fatalf("source was not retained: %v", err)
	}
}

func TestExtantDurableLinkCountsUntilCleanupSucceeds(t *testing.T) {
	dir := t.TempDir()
	store, err := journal.Open(filepath.Join(dir, "journal.json"))
	if err != nil {
		t.Fatal(err)
	}
	source, path, info := writeSourceAndSpoolLink(t, dir, "durable", 8)
	if err := store.Update(func(data *state.Journal) error {
		data.Files["durable"] = state.File{
			ID: "durable", SourcePath: source, SpoolPath: path,
			Size: 8, ModTimeNS: info.ModTime().UnixNano(), State: state.FileDurable,
		}
		return nil
	}); err != nil {
		t.Fatal(err)
	}
	guard := New(config.Storage{
		MaxRetainedBytes:  1,
		MinFreeBytes:      1,
		EmergencyBehavior: "release_oldest",
	}, dir, store, discardLogger{})
	status := guard.Enforce()
	if status.RetainedBytes != 0 {
		t.Fatalf("durable hardlink remained counted after cleanup: %#v", status)
	}
	if _, err := os.Stat(path); !os.IsNotExist(err) {
		t.Fatalf("durable hardlink remains: %v", err)
	}
	durable := store.Snapshot().Files["durable"]
	if durable.State != state.FileDurable || durable.NeedsRespool {
		t.Fatalf("cleanup made durable content retryable: %#v", durable)
	}
}

func TestPressureQueuesServerCancelBeforeReleasingActiveLink(t *testing.T) {
	dir := t.TempDir()
	store, err := journal.Open(filepath.Join(dir, "journal.json"))
	if err != nil {
		t.Fatal(err)
	}
	source, path, info := writeSourceAndSpoolLink(t, dir, "active", 8)
	if err := store.Update(func(data *state.Journal) error {
		data.Files["active"] = state.File{
			ID: "active", SourcePath: source, SpoolPath: path,
			Size: 8, ModTimeNS: info.ModTime().UnixNano(), State: state.FileUploading,
			UploadID: "session", SpooledAt: time.Unix(1, 0),
		}
		return nil
	}); err != nil {
		t.Fatal(err)
	}
	guard := New(config.Storage{
		MaxRetainedBytes:  1,
		MinFreeBytes:      1,
		EmergencyBehavior: "release_oldest",
	}, dir, store, discardLogger{})
	status := guard.Enforce()
	if status.ReleasedFiles != 1 || status.RetainedBytes != 0 {
		t.Fatalf("unexpected pressure result: %#v", status)
	}
	snapshot := store.Snapshot()
	file := snapshot.Files["active"]
	if file.State != state.FileCancelPending || file.CancelNextState != state.FileReleased ||
		!file.NeedsRespool {
		t.Fatalf("active upload was not staged for server cleanup: %#v", file)
	}
	if _, ok := snapshot.Cancellations[state.CancellationKey("active", "session")]; !ok {
		t.Fatal("server cancel was not persisted")
	}
	if _, err := os.Stat(path); !os.IsNotExist(err) {
		t.Fatalf("active hardlink remains: %v", err)
	}
}

func TestPressurePreservesRetryAfterPendingServerCancel(t *testing.T) {
	dir := t.TempDir()
	store, err := journal.Open(filepath.Join(dir, "journal.json"))
	if err != nil {
		t.Fatal(err)
	}
	source, spoolPath, info := writeSourceAndSpoolLink(t, dir, "pending-retry", 8)
	if err := store.Update(func(data *state.Journal) error {
		data.Files["pending-retry"] = state.File{
			ID: "pending-retry", SourcePath: source, SpoolPath: spoolPath,
			Size: 8, ModTimeNS: info.ModTime().UnixNano(),
			State: state.FileCancelPending, UploadID: "session",
			CancelNextState: state.FileRetry, SpooledAt: time.Unix(1, 0),
		}
		data.Cancellations[state.CancellationKey("pending-retry", "session")] =
			state.UploadCancellation{
				FileID: "pending-retry", UploadID: "session",
				RequestedAt: time.Unix(1, 0),
			}
		return nil
	}); err != nil {
		t.Fatal(err)
	}
	guard := New(config.Storage{
		MaxRetainedBytes:  1,
		MinFreeBytes:      1,
		EmergencyBehavior: "release_oldest",
	}, dir, store, discardLogger{})
	if status := guard.Enforce(); status.ReleasedFiles != 1 {
		t.Fatalf("unexpected pressure result: %#v", status)
	}
	file := store.Snapshot().Files["pending-retry"]
	if file.State != state.FileCancelPending ||
		file.CancelNextState != state.FileReleased ||
		!file.NeedsRespool {
		t.Fatalf("pending retry lost cancel-before-respool ordering: %#v", file)
	}
}

func TestPressureDoesNotRespoolMissingOrReplacedSource(t *testing.T) {
	for _, test := range []struct {
		name    string
		replace bool
	}{
		{name: "missing"},
		{name: "replaced-same-metadata", replace: true},
	} {
		t.Run(test.name, func(t *testing.T) {
			dir := t.TempDir()
			store, err := journal.Open(filepath.Join(dir, "journal.json"))
			if err != nil {
				t.Fatal(err)
			}
			source, spoolPath, info := writeSourceAndSpoolLink(t, dir, "candidate", 8)
			if err := os.Remove(source); err != nil {
				t.Fatal(err)
			}
			if test.replace {
				if err := os.WriteFile(source, make([]byte, 8), 0o600); err != nil {
					t.Fatal(err)
				}
				if err := os.Chtimes(source, info.ModTime(), info.ModTime()); err != nil {
					t.Fatal(err)
				}
			}
			if err := store.Update(func(data *state.Journal) error {
				data.Files["candidate"] = state.File{
					ID: "candidate", SourcePath: source, SpoolPath: spoolPath,
					Size: 8, ModTimeNS: info.ModTime().UnixNano(),
					State: state.FileRetry, SpooledAt: time.Unix(1, 0),
				}
				return nil
			}); err != nil {
				t.Fatal(err)
			}
			guard := New(config.Storage{
				MaxRetainedBytes:  1,
				MinFreeBytes:      1,
				EmergencyBehavior: "release_oldest",
			}, dir, store, discardLogger{})
			if status := guard.Enforce(); status.ReleasedFiles != 1 {
				t.Fatalf("unexpected pressure result: %#v", status)
			}
			file := store.Snapshot().Files["candidate"]
			if file.State != state.FileReleased || file.NeedsRespool {
				t.Fatalf("missing/replaced source was made retryable: %#v", file)
			}
			if !strings.Contains(file.LastError, "source is missing or changed") {
				t.Fatalf("source loss was not explicit: %q", file.LastError)
			}
		})
	}
}

func TestPressureDoesNotResurrectIntentionalCancellation(t *testing.T) {
	dir := t.TempDir()
	store, err := journal.Open(filepath.Join(dir, "journal.json"))
	if err != nil {
		t.Fatal(err)
	}
	source, spoolPath, info := writeSourceAndSpoolLink(t, dir, "canceled", 8)
	if err := store.Update(func(data *state.Journal) error {
		data.Files["canceled"] = state.File{
			ID: "canceled", SourcePath: source, SpoolPath: spoolPath,
			Size: 8, ModTimeNS: info.ModTime().UnixNano(),
			State: state.FileCanceled, SpooledAt: time.Unix(1, 0),
		}
		return nil
	}); err != nil {
		t.Fatal(err)
	}
	guard := New(config.Storage{
		MaxRetainedBytes:  1,
		MinFreeBytes:      1,
		EmergencyBehavior: "release_oldest",
	}, dir, store, discardLogger{})
	if status := guard.Enforce(); status.ReleasedFiles != 1 {
		t.Fatalf("unexpected pressure result: %#v", status)
	}
	file := store.Snapshot().Files["canceled"]
	if file.State != state.FileReleased || file.NeedsRespool {
		t.Fatalf("intentional cancellation was resurrected: %#v", file)
	}
}

func TestPressureDoesNotRespoolReleasedDurableRecord(t *testing.T) {
	dir := t.TempDir()
	store, err := journal.Open(filepath.Join(dir, "journal.json"))
	if err != nil {
		t.Fatal(err)
	}
	source, spoolPath, info := writeSourceAndSpoolLink(t, dir, "released-durable", 8)
	if err := store.Update(func(data *state.Journal) error {
		data.Files["released-durable"] = state.File{
			ID: "released-durable", SourcePath: source, SpoolPath: spoolPath,
			Size: 8, ModTimeNS: info.ModTime().UnixNano(),
			State: state.FileReleased, NeedsRespool: true,
			DurableAt: time.Unix(1, 0), SpooledAt: time.Unix(1, 0),
		}
		return nil
	}); err != nil {
		t.Fatal(err)
	}
	guard := New(config.Storage{
		MaxRetainedBytes:  1,
		MinFreeBytes:      1,
		EmergencyBehavior: "release_oldest",
	}, dir, store, discardLogger{})
	guard.Enforce()
	file := store.Snapshot().Files["released-durable"]
	if file.State != state.FileReleased || file.NeedsRespool {
		t.Fatalf("previously durable record was made retryable: %#v", file)
	}
}

func TestPressureCleanupDoesNotCountAlreadyRequestedReleaseTwice(t *testing.T) {
	dir := t.TempDir()
	store, err := journal.Open(filepath.Join(dir, "journal.json"))
	if err != nil {
		t.Fatal(err)
	}
	path := writeSpoolFile(t, dir, "pending-release.data", 8)
	if err := store.Update(func(data *state.Journal) error {
		data.Files["active"] = state.File{
			ID: "active", SpoolPath: path, Size: 8,
			State: state.FileCancelPending, UploadID: "session",
			CancelNextState: state.FileReleased, ReleasedAt: time.Unix(1, 0),
		}
		data.Counters.FilesReleased = 1
		return nil
	}); err != nil {
		t.Fatal(err)
	}
	guard := New(config.Storage{
		MaxRetainedBytes:  1,
		MinFreeBytes:      1,
		EmergencyBehavior: "release_oldest",
	}, dir, store, discardLogger{})
	status := guard.Enforce()
	if status.ReleasedFiles != 0 || store.Snapshot().Counters.FilesReleased != 1 {
		t.Fatalf("already-requested release was counted again: %#v", status)
	}
	if _, err := os.Stat(path); !os.IsNotExist(err) {
		t.Fatalf("pending release link remains: %v", err)
	}
}

func TestQuarantineBytesCountTowardRetentionLimitWithoutDeletion(t *testing.T) {
	dir := t.TempDir()
	store, err := journal.Open(filepath.Join(dir, "journal.json"))
	if err != nil {
		t.Fatal(err)
	}
	quarantine := filepath.Join(dir, "quarantine", "orphan.data")
	if err := os.MkdirAll(filepath.Dir(quarantine), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(quarantine, make([]byte, 8), 0o600); err != nil {
		t.Fatal(err)
	}
	guard := New(config.Storage{
		MaxRetainedBytes:  4,
		MinFreeBytes:      1,
		EmergencyBehavior: "release_oldest",
	}, dir, store, discardLogger{})
	status := guard.Enforce()
	if status.QuarantineBytes != 8 || status.QuarantineFiles != 1 ||
		status.AllowNew || !status.Pressure {
		t.Fatalf("quarantine did not block new retention: %#v", status)
	}
	if _, err := os.Stat(quarantine); err != nil {
		t.Fatalf("quarantine was deleted automatically: %v", err)
	}
}

func TestPressureNeverDeletesJournalPathOutsideSpoolFiles(t *testing.T) {
	dir := t.TempDir()
	store, err := journal.Open(filepath.Join(dir, "journal.json"))
	if err != nil {
		t.Fatal(err)
	}
	outside := filepath.Join(dir, "source.data")
	if err := os.WriteFile(outside, make([]byte, 8), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := store.Update(func(data *state.Journal) error {
		data.Files["unsafe"] = state.File{
			ID: "unsafe", SpoolPath: outside, Size: 8, State: state.FileSpooled,
			SpooledAt: time.Unix(1, 0),
		}
		return nil
	}); err != nil {
		t.Fatal(err)
	}
	guard := New(config.Storage{
		MaxRetainedBytes:  1,
		MinFreeBytes:      1,
		EmergencyBehavior: "release_oldest",
	}, dir, store, discardLogger{})
	status := guard.Enforce()
	if status.ReleasedFiles != 0 || status.RetainedBytes != 0 {
		t.Fatalf("outside path was treated as retained spool data: %#v", status)
	}
	if _, err := os.Stat(outside); err != nil {
		t.Fatalf("outside path was deleted: %v", err)
	}
}

func writeSpoolFile(t *testing.T, spool, name string, size int) string {
	t.Helper()
	path := filepath.Join(spool, "files", name)
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, make([]byte, size), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

func writeSourceAndSpoolLink(
	t *testing.T,
	spool string,
	name string,
	size int,
) (string, string, os.FileInfo) {
	t.Helper()
	source := filepath.Join(spool, "sources", name+".data")
	if err := os.MkdirAll(filepath.Dir(source), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(source, make([]byte, size), 0o600); err != nil {
		t.Fatal(err)
	}
	info, err := os.Stat(source)
	if err != nil {
		t.Fatal(err)
	}
	spoolPath := filepath.Join(spool, "files", name+".data")
	if err := os.MkdirAll(filepath.Dir(spoolPath), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.Link(source, spoolPath); err != nil {
		t.Fatal(err)
	}
	return source, spoolPath, info
}

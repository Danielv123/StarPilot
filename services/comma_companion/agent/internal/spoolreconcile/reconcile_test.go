package spoolreconcile

import (
	"os"
	"path/filepath"
	"testing"
	"time"

	"starpilot.local/comma-companion-agent/internal/journal"
	"starpilot.local/comma-companion-agent/internal/state"
)

type quietLogger struct{}

func (quietLogger) Printf(string, ...any) {}

func TestReconcileRemovesDurableLinkAfterJournalBeforeUnlinkCrash(t *testing.T) {
	dir := t.TempDir()
	source := filepath.Join(dir, "source")
	writeFile(t, source, "durable")
	info := statFile(t, source)
	spoolPath := filepath.Join(dir, "spool", "files", "aa", "bb", "file.data")
	linkFile(t, source, spoolPath)
	store := openStore(t, dir)
	putFile(t, store, state.File{
		ID: "file", SourcePath: source, SpoolPath: spoolPath, Size: info.Size(),
		ModTimeNS: info.ModTime().UnixNano(), State: state.FileDurable,
	})

	result, err := Reconcile(filepath.Join(dir, "spool"), store, quietLogger{}, 100)
	if err != nil {
		t.Fatal(err)
	}
	if result.TerminalLinksRemoved != 1 {
		t.Fatalf("terminal links removed = %d", result.TerminalLinksRemoved)
	}
	if _, err := os.Lstat(spoolPath); !os.IsNotExist(err) {
		t.Fatalf("durable spool link remains: %v", err)
	}
	if got := string(readFile(t, source)); got != "durable" {
		t.Fatalf("source changed: %q", got)
	}
}

func TestReconcileQuarantinesUnknownScannerCrashLink(t *testing.T) {
	dir := t.TempDir()
	source := filepath.Join(dir, "source")
	writeFile(t, source, "orphan")
	orphan := filepath.Join(dir, "spool", "files", "aa", "bb", "orphan.data")
	linkFile(t, source, orphan)
	store := openStore(t, dir)

	result, err := Reconcile(filepath.Join(dir, "spool"), store, quietLogger{}, 100)
	if err != nil {
		t.Fatal(err)
	}
	if result.OrphansQuarantined != 1 {
		t.Fatalf("orphans quarantined = %d", result.OrphansQuarantined)
	}
	if _, err := os.Lstat(orphan); !os.IsNotExist(err) {
		t.Fatalf("orphan remained in active spool: %v", err)
	}
	entries, err := os.ReadDir(filepath.Join(dir, "spool", "quarantine"))
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 1 {
		t.Fatalf("quarantine entries = %d", len(entries))
	}
	if got := string(readFile(t, filepath.Join(dir, "spool", "quarantine", entries[0].Name()))); got != "orphan" {
		t.Fatalf("quarantined content = %q", got)
	}
	if got := string(readFile(t, source)); got != "orphan" {
		t.Fatalf("source changed: %q", got)
	}
}

func TestReconcileAdoptsKnownReleasedRetryLink(t *testing.T) {
	dir := t.TempDir()
	source := filepath.Join(dir, "source")
	writeFile(t, source, "retry")
	info := statFile(t, source)
	spoolPath := filepath.Join(dir, "spool", "files", "aa", "bb", "retry.data")
	linkFile(t, source, spoolPath)
	store := openStore(t, dir)
	putFile(t, store, state.File{
		ID: "file", SourcePath: source, SpoolPath: spoolPath, Size: info.Size(),
		ModTimeNS: info.ModTime().UnixNano(), State: state.FileReleased,
		NeedsRespool: true, UploadAttempt: 4,
	})

	result, err := Reconcile(filepath.Join(dir, "spool"), store, quietLogger{}, 100)
	if err != nil {
		t.Fatal(err)
	}
	if result.Adopted != 1 {
		t.Fatalf("adopted = %d", result.Adopted)
	}
	file := store.Snapshot().Files["file"]
	if file.State != state.FileRetry || file.NeedsRespool {
		t.Fatalf("unexpected adopted state: %#v", file)
	}
	if file.UploadAttempt != 4 {
		t.Fatalf("upload attempt changed: %d", file.UploadAttempt)
	}
	if _, err := os.Lstat(spoolPath); err != nil {
		t.Fatalf("adopted spool missing: %v", err)
	}
}

func TestReconcileMissingActiveLinkQueuesServerCancellation(t *testing.T) {
	dir := t.TempDir()
	source := filepath.Join(dir, "source")
	writeFile(t, source, "missing")
	info := statFile(t, source)
	spoolPath := filepath.Join(dir, "spool", "files", "aa", "bb", "missing.data")
	store := openStore(t, dir)
	putFile(t, store, state.File{
		ID: "file", SourcePath: source, SpoolPath: spoolPath, Size: info.Size(),
		ModTimeNS: info.ModTime().UnixNano(), State: state.FileUploading,
		UploadID: "upload", UploadAttempt: 2,
	})

	if _, err := Reconcile(filepath.Join(dir, "spool"), store, quietLogger{}, 100); err != nil {
		t.Fatal(err)
	}
	snapshot := store.Snapshot()
	file := snapshot.Files["file"]
	if file.State != state.FileCancelPending || file.CancelNextState != state.FileReleased ||
		!file.NeedsRespool {
		t.Fatalf("unexpected missing-link state: %#v", file)
	}
	if _, ok := snapshot.Cancellations[state.CancellationKey("file", "upload")]; !ok {
		t.Fatal("server cancellation was not persisted")
	}
}

func TestReconcileSanitizesJournalSpoolPathOutsideActiveTree(t *testing.T) {
	dir := t.TempDir()
	source := filepath.Join(dir, "source")
	writeFile(t, source, "protected")
	info := statFile(t, source)
	store := openStore(t, dir)
	putFile(t, store, state.File{
		ID: "file", SourcePath: source, SpoolPath: source, Size: info.Size(),
		ModTimeNS: info.ModTime().UnixNano(), State: state.FileUploading,
		UploadID: "upload",
	})

	if _, err := Reconcile(filepath.Join(dir, "spool"), store, quietLogger{}, 100); err != nil {
		t.Fatal(err)
	}
	if got := string(readFile(t, source)); got != "protected" {
		t.Fatalf("source was changed through unsafe spool path: %q", got)
	}
	file := store.Snapshot().Files["file"]
	filesRoot := filepath.Join(dir, "spool", "files")
	if !within(filesRoot, file.SpoolPath) {
		t.Fatalf("unsafe spool path was retained: %q", file.SpoolPath)
	}
	if file.State != state.FileCancelPending || file.CancelNextState != state.FileReleased {
		t.Fatalf("unsafe active record was not made non-uploadable: %#v", file)
	}
}

func openStore(t *testing.T, dir string) *journal.Store {
	t.Helper()
	store, err := journal.Open(filepath.Join(dir, "spool", "journal.json"))
	if err != nil {
		t.Fatal(err)
	}
	return store
}

func putFile(t *testing.T, store *journal.Store, file state.File) {
	t.Helper()
	if file.FirstObserved.IsZero() {
		file.FirstObserved = time.Now().UTC()
	}
	if err := store.Update(func(data *state.Journal) error {
		data.Files[file.ID] = file
		return nil
	}); err != nil {
		t.Fatal(err)
	}
}

func writeFile(t *testing.T, path, value string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(value), 0o600); err != nil {
		t.Fatal(err)
	}
}

func linkFile(t *testing.T, source, target string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(target), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.Link(source, target); err != nil {
		t.Fatal(err)
	}
}

func statFile(t *testing.T, path string) os.FileInfo {
	t.Helper()
	info, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	return info
}

func readFile(t *testing.T, path string) []byte {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return data
}

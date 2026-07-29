package journal

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"starpilot.local/comma-companion-agent/internal/state"
)

func TestJournalPersistsUpdates(t *testing.T) {
	path := filepath.Join(t.TempDir(), "state", "journal.json")
	store, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	if err := store.Update(func(data *state.Journal) error {
		data.Paused = true
		data.Files["one"] = state.File{ID: "one", State: state.FileSpooled}
		return nil
	}); err != nil {
		t.Fatal(err)
	}
	reopened, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	snapshot := reopened.Snapshot()
	if !snapshot.Paused {
		t.Fatal("paused state was not persisted")
	}
	if snapshot.Files["one"].State != state.FileSpooled {
		t.Fatalf("unexpected file state: %#v", snapshot.Files["one"])
	}
}

func TestVersionOneJournalMigratesUploadAttempts(t *testing.T) {
	path := filepath.Join(t.TempDir(), "journal.json")
	legacy := state.EmptyJournal()
	legacy.Version = 1
	legacy.Files["file"] = state.File{
		ID:       "file",
		UploadID: "stale-session",
		State:    state.FileRetry,
	}
	raw, err := json.Marshal(legacy)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, raw, 0o600); err != nil {
		t.Fatal(err)
	}
	store, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	snapshot := store.Snapshot()
	if snapshot.Version != state.CurrentVersion {
		t.Fatalf("journal remained at version %d", snapshot.Version)
	}
	if snapshot.Files["file"].UploadAttempt != 0 || snapshot.Files["file"].UploadID != "stale-session" {
		t.Fatalf("migration changed existing upload identity: %#v", snapshot.Files["file"])
	}
	if snapshot.Cancellations == nil {
		t.Fatal("migration did not initialize durable cancellation queue")
	}
	reopened, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	if reopened.Snapshot().Version != state.CurrentVersion {
		t.Fatal("migrated version was not persisted")
	}
}

func TestVersionTwoJournalMigratesCancellationQueue(t *testing.T) {
	path := filepath.Join(t.TempDir(), "journal.json")
	raw := []byte(`{
		"version":2,
		"observations":{},
		"files":{"file":{"id":"file","state":"cancel_pending","upload_id":"session"}},
		"commands":{},
		"counters":{}
	}`)
	if err := os.WriteFile(path, raw, 0o600); err != nil {
		t.Fatal(err)
	}
	store, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	snapshot := store.Snapshot()
	if snapshot.Version != state.CurrentVersion || snapshot.Cancellations == nil {
		t.Fatalf("version two migration incomplete: %#v", snapshot)
	}
}

func TestVersionThreeJournalMigratesRouteInventoryState(t *testing.T) {
	path := filepath.Join(t.TempDir(), "journal.json")
	raw := []byte(`{
		"version":3,
		"observations":{},
		"files":{},
		"upload_cancellations":{},
		"commands":{},
		"counters":{}
	}`)
	if err := os.WriteFile(path, raw, 0o600); err != nil {
		t.Fatal(err)
	}
	store, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	snapshot := store.Snapshot()
	if snapshot.Version != state.CurrentVersion || snapshot.Inventories == nil {
		t.Fatalf("version three route inventory migration incomplete: %#v", snapshot)
	}
}

func TestFailedUpdateDoesNotCommit(t *testing.T) {
	path := filepath.Join(t.TempDir(), "journal.json")
	store, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	expected := assertError("stop")
	err = store.Update(func(data *state.Journal) error {
		data.Paused = true
		return expected
	})
	if err != expected {
		t.Fatalf("got %v, expected %v", err, expected)
	}
	if store.Snapshot().Paused {
		t.Fatal("failed update mutated in-memory journal")
	}
}

func TestCompactionRemovesOnlyTerminalRecordsWithNoRemainingLinks(t *testing.T) {
	dir := t.TempDir()
	store, err := Open(filepath.Join(dir, "journal.json"))
	if err != nil {
		t.Fatal(err)
	}
	retainedSource := filepath.Join(dir, "retained-source")
	if err := os.WriteFile(retainedSource, []byte("keep"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := store.Update(func(data *state.Journal) error {
		data.Files["gone"] = state.File{
			ID: "gone", SourcePath: filepath.Join(dir, "gone-source"),
			SpoolPath: filepath.Join(dir, "gone-spool"), State: state.FileDurable,
		}
		data.Observations[filepath.Join(dir, "gone-source")] = state.Observation{
			SourcePath: filepath.Join(dir, "gone-source"),
		}
		data.Files["retained"] = state.File{
			ID: "retained", SourcePath: retainedSource,
			SpoolPath: filepath.Join(dir, "missing-spool"), State: state.FileDurable,
		}
		data.Files["active"] = state.File{
			ID: "active", SourcePath: filepath.Join(dir, "missing-active"),
			SpoolPath: filepath.Join(dir, "missing-active-spool"), State: state.FileRetry,
		}
		return nil
	}); err != nil {
		t.Fatal(err)
	}
	removed, err := store.CompactTerminalRecords(256)
	if err != nil {
		t.Fatal(err)
	}
	if removed != 1 {
		t.Fatalf("removed = %d", removed)
	}
	snapshot := store.Snapshot()
	if _, exists := snapshot.Files["gone"]; exists {
		t.Fatal("gone terminal record was retained")
	}
	if _, exists := snapshot.Observations[filepath.Join(dir, "gone-source")]; exists {
		t.Fatal("gone observation was retained")
	}
	if _, exists := snapshot.Files["retained"]; !exists {
		t.Fatal("terminal record with source was removed")
	}
	if _, exists := snapshot.Files["active"]; !exists {
		t.Fatal("non-terminal record was removed")
	}
	if snapshot.Counters.FilesCompacted != 1 {
		t.Fatalf("compaction counter = %d", snapshot.Counters.FilesCompacted)
	}
}

func TestCompactionRetainsSegmentRecordUntilCapturedByInventory(t *testing.T) {
	dir := t.TempDir()
	store, err := Open(filepath.Join(dir, "journal.json"))
	if err != nil {
		t.Fatal(err)
	}
	segment := 0
	file := state.File{
		ID:            "route-file",
		SourcePath:    filepath.Join(dir, "missing-source"),
		SpoolPath:     filepath.Join(dir, "missing-spool"),
		RootName:      "realdata",
		RelativePath:  "realdata/route--0/rlog.zst",
		RouteName:     "route",
		SegmentNumber: &segment,
		ArtifactType:  "rlog",
		Size:          10,
		ModTimeNS:     100,
		SHA256:        strings.Repeat("a", 64),
		State:         state.FileDurable,
		DurableAt:     time.Now().UTC(),
	}
	if err := store.Update(func(data *state.Journal) error {
		data.Files[file.ID] = file
		return nil
	}); err != nil {
		t.Fatal(err)
	}
	if removed, err := store.CompactTerminalRecords(256); err != nil || removed != 0 {
		t.Fatalf("uncaptured route file was compacted: removed=%d err=%v", removed, err)
	}
	if err := store.Update(func(data *state.Journal) error {
		data.Inventories["manifest"] = state.RouteInventory{
			ManifestSHA256: "manifest",
			Manifest: state.RouteManifest{
				RouteName: "route",
				Segments: []state.InventorySegment{{
					Number: 0,
					Files: []state.InventoryFile{{
						ArtifactType: "rlog",
						MTimeNS:      file.ModTimeNS,
						RelativePath: file.RelativePath,
						SHA256:       file.SHA256,
						Size:         file.Size,
					}},
				}},
			},
		}
		return nil
	}); err != nil {
		t.Fatal(err)
	}
	if removed, err := store.CompactTerminalRecords(256); err != nil || removed != 1 {
		t.Fatalf("captured route file did not compact: removed=%d err=%v", removed, err)
	}
}

func TestSnapshotDeepClonesRouteInventoryPointers(t *testing.T) {
	store, err := Open(filepath.Join(t.TempDir(), "journal.json"))
	if err != nil {
		t.Fatal(err)
	}
	camera := "road"
	path := "realdata/route--0/fcamera.hevc"
	size := int64(10)
	if err := store.Update(func(data *state.Journal) error {
		data.Inventories["manifest"] = state.RouteInventory{
			Manifest: state.RouteManifest{
				PreviousManifestSHA256: &path,
				ExpectedStreams: []state.ExpectedStream{{
					Camera: &camera,
				}},
				Segments: []state.InventorySegment{{
					Streams: []state.InventoryStream{{
						RelativePath: &path,
						Size:         &size,
					}},
				}},
			},
		}
		return nil
	}); err != nil {
		t.Fatal(err)
	}
	snapshot := store.Snapshot()
	record := snapshot.Inventories["manifest"]
	*record.Manifest.PreviousManifestSHA256 = "changed"
	*record.Manifest.ExpectedStreams[0].Camera = "changed"
	*record.Manifest.Segments[0].Streams[0].RelativePath = "changed"
	*record.Manifest.Segments[0].Streams[0].Size = 99
	reloaded := store.Snapshot().Inventories["manifest"].Manifest
	if *reloaded.PreviousManifestSHA256 != path ||
		*reloaded.ExpectedStreams[0].Camera != camera ||
		*reloaded.Segments[0].Streams[0].RelativePath != path ||
		*reloaded.Segments[0].Streams[0].Size != size {
		t.Fatal("snapshot mutation leaked through route inventory pointers")
	}
}

type assertError string

func (e assertError) Error() string {
	return string(e)
}

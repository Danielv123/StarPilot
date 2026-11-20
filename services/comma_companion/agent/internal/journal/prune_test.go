package journal

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"starpilot.local/comma-companion-agent/internal/state"
)

func pruneTestInventory(route string, generation int, declared bool, files ...string) state.RouteInventory {
	segment := state.InventorySegment{Number: 0}
	for _, name := range files {
		segment.Files = append(segment.Files, state.InventoryFile{
			ArtifactType: "rlog",
			MTimeNS:      100,
			RelativePath: "realdata/" + route + "--0/" + name,
			SHA256:       strings.Repeat("a", 64),
			Size:         10,
		})
	}
	record := state.RouteInventory{
		ManifestSHA256: fmt.Sprintf("%s-%d", route, generation),
		Manifest: state.RouteManifest{
			RouteName:  route,
			Generation: generation,
			Segments:   []state.InventorySegment{segment},
		},
		CapturedAt: time.Unix(int64(generation), 0).UTC(),
	}
	if declared {
		record.DeclaredAt = time.Unix(int64(generation)+1, 0).UTC()
	}
	return record
}

func pruneTestStore(t *testing.T, records ...state.RouteInventory) (*Store, string) {
	t.Helper()
	path := filepath.Join(t.TempDir(), "journal.json")
	store, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	if err := store.Update(func(data *state.Journal) error {
		for _, record := range records {
			data.Inventories[record.ManifestSHA256] = record
		}
		return nil
	}); err != nil {
		t.Fatal(err)
	}
	return store, path
}

func inventoryKeys(store *Store) map[string]bool {
	keys := make(map[string]bool)
	for key := range store.View().Inventories {
		keys[key] = true
	}
	return keys
}

func TestPruneDropsDeclaredGenerationsBelowNewestDeclared(t *testing.T) {
	store, _ := pruneTestStore(t,
		pruneTestInventory("route", 1, true, "qlog"),
		pruneTestInventory("route", 2, true, "qlog", "rlog"),
		pruneTestInventory("route", 3, true, "qlog", "rlog", "fcamera.hevc"),
		pruneTestInventory("route", 4, false, "qlog", "rlog", "fcamera.hevc", "ecamera.hevc"),
		pruneTestInventory("other", 1, true, "qlog"),
	)
	removed, err := store.PruneSupersededInventories()
	if err != nil || removed != 2 {
		t.Fatalf("removed=%d err=%v", removed, err)
	}
	keys := inventoryKeys(store)
	// Generation 3 is the newest the server holds and generation 4 still has
	// to be sent, so both stay; a single-generation route is untouched.
	for _, want := range []string{"route-3", "route-4", "other-1"} {
		if !keys[want] {
			t.Fatalf("%s was pruned; remaining %v", want, keys)
		}
	}
	if len(keys) != 3 {
		t.Fatalf("unexpected remaining inventories %v", keys)
	}
	if removed, err := store.PruneSupersededInventories(); err != nil || removed != 0 {
		t.Fatalf("second prune removed=%d err=%v", removed, err)
	}
}

func TestPruneKeepsEverythingUntilANewerGenerationIsDeclared(t *testing.T) {
	store, _ := pruneTestStore(t,
		pruneTestInventory("route", 1, true, "qlog"),
		pruneTestInventory("route", 2, false, "qlog", "rlog"),
	)
	if removed, err := store.PruneSupersededInventories(); err != nil || removed != 0 {
		t.Fatalf("removed=%d err=%v", removed, err)
	}
	if len(store.View().Inventories) != 2 {
		t.Fatal("the only declared generation was pruned")
	}
}

func TestPruneKeepsGenerationThatAloneCapturesARemainingFileRecord(t *testing.T) {
	// Terminal-record compaction needs each remaining file record to appear in
	// a retained manifest. Generation 1 is the only one listing the rlog.
	store, _ := pruneTestStore(t,
		pruneTestInventory("route", 1, true, "qlog", "rlog"),
		pruneTestInventory("route", 2, true, "qlog"),
		pruneTestInventory("route", 3, true, "qlog"),
	)
	segment := 0
	if err := store.Update(func(data *state.Journal) error {
		data.Files["rlog"] = state.File{
			ID:            "rlog",
			RouteName:     "route",
			SegmentNumber: &segment,
			RelativePath:  "realdata/route--0/rlog",
			Size:          10,
			ModTimeNS:     100,
			SHA256:        strings.Repeat("a", 64),
			State:         state.FileDurable,
		}
		return nil
	}); err != nil {
		t.Fatal(err)
	}
	removed, err := store.PruneSupersededInventories()
	if err != nil || removed != 1 {
		t.Fatalf("removed=%d err=%v", removed, err)
	}
	keys := inventoryKeys(store)
	if !keys["route-1"] || !keys["route-3"] || keys["route-2"] {
		t.Fatalf("unexpected remaining inventories %v", keys)
	}
	if removed, err := store.CompactTerminalRecords(256); err != nil || removed != 1 {
		t.Fatalf("captured record did not compact: removed=%d err=%v", removed, err)
	}
	// With the record gone, nothing reads generation 1 any more.
	if removed, err := store.PruneSupersededInventories(); err != nil || removed != 1 {
		t.Fatalf("prune after compaction removed=%d err=%v", removed, err)
	}
	if keys := inventoryKeys(store); len(keys) != 1 || !keys["route-3"] {
		t.Fatalf("unexpected remaining inventories %v", keys)
	}
}

func TestOpenPrunesAndPersistsSupersededInventories(t *testing.T) {
	store, path := pruneTestStore(t,
		pruneTestInventory("route", 1, true, "qlog"),
		pruneTestInventory("route", 2, true, "qlog", "rlog"),
	)
	before, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	_ = store
	reopened, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	if reopened.PrunedOnOpen() != 1 || len(reopened.View().Inventories) != 1 {
		t.Fatalf("pruned=%d remaining=%d", reopened.PrunedOnOpen(), len(reopened.View().Inventories))
	}
	after, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	if after.Size() >= before.Size() {
		t.Fatalf("pruned journal was not persisted: %d -> %d bytes", before.Size(), after.Size())
	}
	again, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	if again.PrunedOnOpen() != 0 || len(again.View().Inventories) != 1 {
		t.Fatalf("second open pruned=%d remaining=%d", again.PrunedOnOpen(), len(again.View().Inventories))
	}
}

func TestRemoveStaleTemporariesLeavesJournalAndWAL(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "journal.json")
	keep := []string{"journal.json", "journal.json.wal", "agent.lock", ".journal-keep.txt"}
	stale := []string{".journal-1055672027.tmp", ".journal-7.tmp"}
	for _, name := range append(append([]string{}, keep...), stale...) {
		if err := os.WriteFile(filepath.Join(dir, name), []byte("x"), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.Mkdir(filepath.Join(dir, ".journal-directory.tmp"), 0o700); err != nil {
		t.Fatal(err)
	}
	removed, err := RemoveStaleTemporaries(path)
	if err != nil || removed != len(stale) {
		t.Fatalf("removed=%d err=%v", removed, err)
	}
	for _, name := range append(keep, ".journal-directory.tmp") {
		if _, err := os.Lstat(filepath.Join(dir, name)); err != nil {
			t.Fatalf("%s was removed: %v", name, err)
		}
	}
	for _, name := range stale {
		if _, err := os.Lstat(filepath.Join(dir, name)); !os.IsNotExist(err) {
			t.Fatalf("%s survived: %v", name, err)
		}
	}
	if removed, err := RemoveStaleTemporaries(filepath.Join(dir, "missing", "journal.json")); err != nil || removed != 0 {
		t.Fatalf("missing directory: removed=%d err=%v", removed, err)
	}
}

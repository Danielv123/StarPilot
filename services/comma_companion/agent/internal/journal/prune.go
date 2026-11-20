package journal

import (
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"starpilot.local/comma-companion-agent/internal/state"
)

// RemoveStaleTemporaries deletes checkpoint temporaries that an earlier
// process left beside the journal when it was killed between CreateTemp and
// rename. Each one is a complete journal copy, so a kill loop can strand
// gigabytes. The caller must hold the single-instance process lock: a live
// checkpoint uses the same name pattern.
func RemoveStaleTemporaries(journalPath string) (int, error) {
	directory := filepath.Dir(journalPath)
	entries, err := os.ReadDir(directory)
	if errors.Is(err, fs.ErrNotExist) {
		return 0, nil
	}
	if err != nil {
		return 0, fmt.Errorf("list journal directory: %w", err)
	}
	removed := 0
	var firstErr error
	for _, entry := range entries {
		name := entry.Name()
		if !entry.Type().IsRegular() ||
			!strings.HasPrefix(name, ".journal-") ||
			!strings.HasSuffix(name, ".tmp") {
			continue
		}
		if err := os.Remove(filepath.Join(directory, name)); err != nil {
			if !errors.Is(err, fs.ErrNotExist) && firstErr == nil {
				firstErr = fmt.Errorf("remove stale journal temporary %s: %w", name, err)
			}
			continue
		}
		removed++
	}
	return removed, firstErr
}

// PrunedOnOpen reports how many superseded route inventories Open dropped.
func (s *Store) PrunedOnOpen() int {
	return s.prunedOnOpen
}

// PruneSupersededInventories drops route-inventory generations the server has
// already replaced. Every hashed file produces a new immutable generation, so
// one long route leaves dozens of full manifests behind; without pruning they
// are nearly all of the journal and are rewritten on every update.
func (s *Store) PruneSupersededInventories() (int, error) {
	if len(supersededInventoryKeys(s.View())) == 0 {
		return 0, nil
	}
	removed := 0
	err := s.Update(func(data *state.Journal) error {
		removed = 0
		for _, key := range supersededInventoryKeys(*data) {
			delete(data.Inventories, key)
			removed++
		}
		return nil
	})
	if err != nil {
		return 0, err
	}
	return removed, nil
}

type inventoryFileKey struct {
	relativePath string
	size         int64
	mtimeNS      int64
	sha256       string
}

// supersededInventoryKeys selects generations that nothing reads any more.
// Only the newest generation of a route seeds the next one, only undeclared
// generations are sent, and terminal-record compaction needs each remaining
// file record to appear in some retained manifest. A generation is therefore
// removable once it is declared, a newer generation is declared too, and the
// retained generations still list every file of it that has a journal record.
func supersededInventoryKeys(data state.Journal) []string {
	live := make(map[inventoryFileKey]struct{})
	for _, file := range data.Files {
		if file.RouteName == "" || file.SegmentNumber == nil {
			continue
		}
		live[inventoryFileKey{
			relativePath: file.RelativePath,
			size:         file.Size,
			mtimeNS:      file.ModTimeNS,
			sha256:       file.SHA256,
		}] = struct{}{}
	}
	type entry struct {
		key    string
		record state.RouteInventory
	}
	routes := make(map[string][]entry)
	for key, record := range data.Inventories {
		if record.Manifest.RouteName == "" {
			continue
		}
		routes[record.Manifest.RouteName] = append(
			routes[record.Manifest.RouteName],
			entry{key: key, record: record},
		)
	}
	var prunable []string
	for _, entries := range routes {
		if len(entries) < 2 {
			continue
		}
		sort.Slice(entries, func(i, j int) bool {
			left, right := entries[i].record.Manifest, entries[j].record.Manifest
			if left.Generation == right.Generation {
				return entries[i].key < entries[j].key
			}
			return left.Generation > right.Generation
		})
		newestDeclared := -1
		for index, candidate := range entries {
			if !candidate.record.DeclaredAt.IsZero() {
				newestDeclared = index
				break
			}
		}
		if newestDeclared < 0 {
			continue
		}
		retained := make(map[inventoryFileKey]struct{})
		for _, kept := range entries[:newestDeclared+1] {
			addInventoryFiles(retained, kept.record.Manifest)
		}
		newestGeneration := entries[newestDeclared].record.Manifest.Generation
		for _, candidate := range entries[newestDeclared+1:] {
			manifest := candidate.record.Manifest
			if candidate.record.DeclaredAt.IsZero() ||
				manifest.Generation >= newestGeneration ||
				!inventoryFilesCovered(retained, live, manifest) {
				addInventoryFiles(retained, manifest)
				continue
			}
			prunable = append(prunable, candidate.key)
		}
	}
	sort.Strings(prunable)
	return prunable
}

func addInventoryFiles(target map[inventoryFileKey]struct{}, manifest state.RouteManifest) {
	visitInventoryFiles(manifest, func(key inventoryFileKey) bool {
		target[key] = struct{}{}
		return true
	})
}

func inventoryFilesCovered(
	retained map[inventoryFileKey]struct{},
	live map[inventoryFileKey]struct{},
	manifest state.RouteManifest,
) bool {
	return visitInventoryFiles(manifest, func(key inventoryFileKey) bool {
		if _, needed := live[key]; !needed {
			return true
		}
		_, found := retained[key]
		return found
	})
}

func visitInventoryFiles(manifest state.RouteManifest, visit func(inventoryFileKey) bool) bool {
	keyOf := func(file state.InventoryFile) inventoryFileKey {
		return inventoryFileKey{
			relativePath: file.RelativePath,
			size:         file.Size,
			mtimeNS:      file.MTimeNS,
			sha256:       file.SHA256,
		}
	}
	for _, file := range manifest.RouteFiles {
		if !visit(keyOf(file)) {
			return false
		}
	}
	for _, segment := range manifest.Segments {
		for _, file := range segment.Files {
			if !visit(keyOf(file)) {
				return false
			}
		}
	}
	return true
}

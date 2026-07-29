package journal

import (
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"sync"
	"time"

	"starpilot.local/comma-companion-agent/internal/state"
)

type Store struct {
	path string
	mu   sync.RWMutex
	data state.Journal
}

func Open(path string) (*Store, error) {
	store := &Store{path: path, data: state.EmptyJournal()}
	raw, err := os.ReadFile(path)
	if errors.Is(err, fs.ErrNotExist) {
		if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
			return nil, fmt.Errorf("create journal directory: %w", err)
		}
		if err := store.persistLocked(); err != nil {
			return nil, err
		}
		return store, nil
	}
	if err != nil {
		return nil, fmt.Errorf("read journal: %w", err)
	}
	if err := json.Unmarshal(raw, &store.data); err != nil {
		return nil, fmt.Errorf("parse journal %s: %w", path, err)
	}
	store.data.Normalize()
	switch store.data.Version {
	case 1, 2, 3:
		store.data.Version = state.CurrentVersion
		migrateUploadCancellations(&store.data)
		if err := store.persistLocked(); err != nil {
			return nil, fmt.Errorf("migrate journal to version %d: %w", state.CurrentVersion, err)
		}
	case state.CurrentVersion:
		if migrateUploadCancellations(&store.data) {
			if err := store.persistLocked(); err != nil {
				return nil, fmt.Errorf("repair upload cancellation queue: %w", err)
			}
		}
	default:
		return nil, fmt.Errorf("unsupported journal version %d", store.data.Version)
	}
	return store, nil
}

func migrateUploadCancellations(data *state.Journal) bool {
	changed := false
	for id, file := range data.Files {
		if file.UploadAttempt < 0 {
			file.UploadAttempt = 0
			data.Files[id] = file
			changed = true
		}
		if file.State != state.FileCancelPending || file.UploadID == "" {
			continue
		}
		if file.CancelNextState == "" && !file.CancelDeleteRecord {
			file.CancelNextState = state.FileCanceled
			data.Files[id] = file
			changed = true
		}
		key := state.CancellationKey(file.ID, file.UploadID)
		if _, exists := data.Cancellations[key]; exists {
			continue
		}
		requestedAt := file.CancelRequestedAt
		data.Cancellations[key] = state.UploadCancellation{
			FileID:      file.ID,
			UploadID:    file.UploadID,
			RequestedAt: requestedAt,
		}
		changed = true
	}
	return changed
}

func (s *Store) Snapshot() state.Journal {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return clone(s.data)
}

func (s *Store) Update(update func(*state.Journal) error) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	next := clone(s.data)
	if err := update(&next); err != nil {
		return err
	}
	next.Normalize()
	if err := persist(s.path, next); err != nil {
		return err
	}
	s.data = next
	return nil
}

func (s *Store) CompactTerminalRecords(limit int) (int, error) {
	if limit <= 0 {
		limit = 256
	}
	snapshot := s.Snapshot()
	type candidate struct {
		id       string
		finished time.Time
	}
	candidates := make([]candidate, 0)
	for id, file := range snapshot.Files {
		if !compactableState(file.State) ||
			!pathMissing(file.SourcePath) ||
			!pathMissing(file.SpoolPath) ||
			hasFileCancellation(snapshot, file.ID) ||
			!fileCapturedByInventory(snapshot, file) {
			continue
		}
		finished := file.DurableAt
		if finished.IsZero() {
			finished = file.ReleasedAt
		}
		if finished.IsZero() {
			finished = file.SpooledAt
		}
		candidates = append(candidates, candidate{id: id, finished: finished})
	}
	if len(candidates) == 0 {
		return 0, nil
	}
	sort.Slice(candidates, func(i, j int) bool {
		return candidates[i].finished.Before(candidates[j].finished)
	})
	if len(candidates) > limit {
		candidates = candidates[:limit]
	}
	removed := 0
	err := s.Update(func(data *state.Journal) error {
		for _, candidate := range candidates {
			file, exists := data.Files[candidate.id]
			if !exists || !compactableState(file.State) ||
				!pathMissing(file.SourcePath) ||
				!pathMissing(file.SpoolPath) ||
				hasFileCancellation(*data, file.ID) ||
				!fileCapturedByInventory(*data, file) {
				continue
			}
			delete(data.Files, candidate.id)
			delete(data.Observations, file.SourcePath)
			removed++
		}
		data.Counters.FilesCompacted += int64(removed)
		return nil
	})
	return removed, err
}

func fileCapturedByInventory(data state.Journal, file state.File) bool {
	if file.RouteName == "" || file.SegmentNumber == nil {
		return true
	}
	for _, record := range data.Inventories {
		if record.Manifest.RouteName != file.RouteName {
			continue
		}
		for _, segment := range record.Manifest.Segments {
			if segment.Number != *file.SegmentNumber {
				continue
			}
			for _, captured := range segment.Files {
				if captured.RelativePath == file.RelativePath &&
					captured.Size == file.Size &&
					captured.MTimeNS == file.ModTimeNS &&
					captured.SHA256 == file.SHA256 {
					return true
				}
			}
		}
	}
	return false
}

func compactableState(fileState state.FileState) bool {
	switch fileState {
	case state.FileDurable, state.FileCanceled, state.FileReleased, state.FileFailed:
		return true
	default:
		return false
	}
}

func pathMissing(path string) bool {
	if path == "" {
		return true
	}
	_, err := os.Lstat(path)
	return errors.Is(err, os.ErrNotExist)
}

func hasFileCancellation(data state.Journal, fileID string) bool {
	for _, cancellation := range data.Cancellations {
		if cancellation.FileID == fileID {
			return true
		}
	}
	return false
}

func (s *Store) persistLocked() error {
	return persist(s.path, s.data)
}

func clone(input state.Journal) state.Journal {
	output := input
	output.Observations = make(map[string]state.Observation, len(input.Observations))
	for key, value := range input.Observations {
		output.Observations[key] = value
	}
	output.Files = make(map[string]state.File, len(input.Files))
	for key, value := range input.Files {
		value.CompletionEvidence = append([]string(nil), value.CompletionEvidence...)
		output.Files[key] = value
	}
	output.Cancellations = make(map[string]state.UploadCancellation, len(input.Cancellations))
	for key, value := range input.Cancellations {
		output.Cancellations[key] = value
	}
	output.Inventories = make(map[string]state.RouteInventory, len(input.Inventories))
	for key, value := range input.Inventories {
		value.Manifest = cloneManifest(value.Manifest)
		output.Inventories[key] = value
	}
	output.Commands = make(map[string]state.CommandRecord, len(input.Commands))
	for key, value := range input.Commands {
		output.Commands[key] = value
	}
	return output
}

func cloneManifest(input state.RouteManifest) state.RouteManifest {
	output := input
	output.PreviousManifestSHA256 = cloneStringPointer(input.PreviousManifestSHA256)
	output.ClosureEvidence = cloneSlice(input.ClosureEvidence)
	output.MissingSegmentNumbers = cloneSlice(input.MissingSegmentNumbers)
	output.RootNames = cloneSlice(input.RootNames)
	output.ExpectedStreams = cloneSlice(input.ExpectedStreams)
	for index := range output.ExpectedStreams {
		output.ExpectedStreams[index].Camera = cloneStringPointer(input.ExpectedStreams[index].Camera)
	}
	output.RouteFiles = cloneSlice(input.RouteFiles)
	for index := range output.RouteFiles {
		output.RouteFiles[index].Camera = cloneStringPointer(input.RouteFiles[index].Camera)
	}
	output.Segments = make([]state.InventorySegment, len(input.Segments))
	for index, segment := range input.Segments {
		output.Segments[index] = segment
		output.Segments[index].Files = cloneSlice(segment.Files)
		for fileIndex := range output.Segments[index].Files {
			output.Segments[index].Files[fileIndex].Camera = cloneStringPointer(
				segment.Files[fileIndex].Camera,
			)
		}
		output.Segments[index].Streams = cloneSlice(segment.Streams)
		for streamIndex := range output.Segments[index].Streams {
			source := segment.Streams[streamIndex]
			target := &output.Segments[index].Streams[streamIndex]
			target.MTimeNS = cloneInt64Pointer(source.MTimeNS)
			target.RelativePath = cloneStringPointer(source.RelativePath)
			target.SHA256 = cloneStringPointer(source.SHA256)
			target.Size = cloneInt64Pointer(source.Size)
		}
	}
	return output
}

func cloneSlice[T any](input []T) []T {
	if input == nil {
		return nil
	}
	output := make([]T, len(input))
	copy(output, input)
	return output
}

func cloneStringPointer(value *string) *string {
	if value == nil {
		return nil
	}
	result := *value
	return &result
}

func cloneInt64Pointer(value *int64) *int64 {
	if value == nil {
		return nil
	}
	result := *value
	return &result
}

func persist(path string, data state.Journal) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return fmt.Errorf("create journal directory: %w", err)
	}
	encoded, err := json.Marshal(data)
	if err != nil {
		return fmt.Errorf("encode journal: %w", err)
	}
	tmp, err := os.CreateTemp(filepath.Dir(path), ".journal-*.tmp")
	if err != nil {
		return fmt.Errorf("create temporary journal: %w", err)
	}
	tmpName := tmp.Name()
	defer os.Remove(tmpName)
	if err := tmp.Chmod(0o600); err != nil {
		tmp.Close()
		return fmt.Errorf("chmod temporary journal: %w", err)
	}
	if _, err := tmp.Write(encoded); err != nil {
		tmp.Close()
		return fmt.Errorf("write temporary journal: %w", err)
	}
	if err := tmp.Sync(); err != nil {
		tmp.Close()
		return fmt.Errorf("sync temporary journal: %w", err)
	}
	if err := tmp.Close(); err != nil {
		return fmt.Errorf("close temporary journal: %w", err)
	}
	if err := os.Rename(tmpName, path); err != nil {
		return fmt.Errorf("replace journal: %w", err)
	}
	if dir, err := os.Open(filepath.Dir(path)); err == nil {
		_ = dir.Sync()
		_ = dir.Close()
	}
	return nil
}

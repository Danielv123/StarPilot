package journal

import (
	"bufio"
	"bytes"
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

var ErrFileNotFound = errors.New("journal file does not exist")

const fileMutationVersion = 1

type fileMutation struct {
	Version  int            `json:"version"`
	Sequence uint64         `json:"sequence"`
	FileID   string         `json:"file_id"`
	File     state.File     `json:"file"`
	Counters state.Counters `json:"counters"`
}

func Open(path string) (*Store, error) {
	store := &Store{path: path, data: state.EmptyJournal()}
	file, err := os.Open(path)
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
	decodeErr := decodeJournal(file, &store.data)
	closeErr := file.Close()
	if decodeErr != nil {
		return nil, fmt.Errorf("parse journal %s: %w", path, decodeErr)
	}
	if closeErr != nil {
		return nil, fmt.Errorf("close journal: %w", closeErr)
	}
	store.data.Normalize()
	if err := store.replayFileMutations(); err != nil {
		return nil, err
	}
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

// View returns the current immutable journal generation without copying its
// maps. Callers must treat the result as read-only. Update always clones the
// current generation before changing it, so a view remains stable while a
// later generation is persisted and installed.
func (s *Store) View() state.Journal {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.data
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
	s.clearFileMutationsLocked()
	return nil
}

// UpdateFile durably changes one file record without rewriting the complete
// journal. Mutations are appended to an fsynced write-ahead log and replayed
// at startup. A later full Update checkpoints the current generation and
// clears the log. The callback returns whether it changed the record.
func (s *Store) UpdateFile(
	fileID string,
	update func(*state.File, *state.Counters) (bool, error),
) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	current, exists := s.data.Files[fileID]
	if !exists {
		return fmt.Errorf("%w: %q", ErrFileNotFound, fileID)
	}
	current.CompletionEvidence = append([]string(nil), current.CompletionEvidence...)
	counters := s.data.Counters
	changed, err := update(&current, &counters)
	if err != nil {
		return err
	}
	if !changed {
		return nil
	}
	next := s.data
	next.Files = make(map[string]state.File, len(s.data.Files))
	for key, value := range s.data.Files {
		next.Files[key] = value
	}
	next.Files[fileID] = current
	next.Counters = counters
	next.MutationSequence++
	record := fileMutation{
		Version:  fileMutationVersion,
		Sequence: next.MutationSequence,
		FileID:   fileID,
		File:     current,
		Counters: counters,
	}
	if err := appendFileMutation(s.fileMutationPath(), record); err != nil {
		return err
	}
	s.data = next
	return nil
}

// Checkpoint writes the current generation to the base journal and clears the
// replay log. It is used during graceful shutdown so an older rollback binary,
// which does not know about the WAL, still sees every committed mutation.
func (s *Store) Checkpoint() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.persistLocked()
}

func (s *Store) CompactTerminalRecords(limit int) (int, error) {
	if limit <= 0 {
		limit = 256
	}
	snapshot := s.View()
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
	if err := persist(s.path, s.data); err != nil {
		return err
	}
	s.clearFileMutationsLocked()
	return nil
}

func (s *Store) fileMutationPath() string {
	return s.path + ".wal"
}

func (s *Store) replayFileMutations() error {
	path := s.fileMutationPath()
	raw, err := os.ReadFile(path)
	if errors.Is(err, fs.ErrNotExist) {
		return nil
	}
	if err != nil {
		return fmt.Errorf("read journal file mutations: %w", err)
	}
	// A crash can leave a partial final append without a newline. That record
	// was never acknowledged as durable to the caller, so ignore the tail.
	if len(raw) > 0 && raw[len(raw)-1] != '\n' {
		lastNewline := bytes.LastIndexByte(raw, '\n')
		if lastNewline < 0 {
			return nil
		}
		raw = raw[:lastNewline+1]
	}
	scanner := bufio.NewScanner(bytes.NewReader(raw))
	scanner.Buffer(make([]byte, 4096), 1024*1024)
	line := 0
	for scanner.Scan() {
		line++
		var record fileMutation
		if err := json.Unmarshal(scanner.Bytes(), &record); err != nil {
			return fmt.Errorf("parse journal file mutation line %d: %w", line, err)
		}
		if record.Version != fileMutationVersion {
			return fmt.Errorf(
				"unsupported journal file mutation version %d on line %d",
				record.Version,
				line,
			)
		}
		if record.Sequence <= s.data.MutationSequence {
			continue
		}
		if record.Sequence != s.data.MutationSequence+1 {
			return fmt.Errorf(
				"journal file mutation sequence gap: got %d after %d",
				record.Sequence,
				s.data.MutationSequence,
			)
		}
		s.data.Files[record.FileID] = record.File
		s.data.Counters = record.Counters
		s.data.MutationSequence = record.Sequence
	}
	if err := scanner.Err(); err != nil {
		return fmt.Errorf("scan journal file mutations: %w", err)
	}
	return nil
}

func appendFileMutation(path string, record fileMutation) error {
	encoded, err := json.Marshal(record)
	if err != nil {
		return fmt.Errorf("encode journal file mutation: %w", err)
	}
	encoded = append(encoded, '\n')
	file, err := os.OpenFile(path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600)
	if err != nil {
		return fmt.Errorf("open journal file mutations: %w", err)
	}
	if _, err := file.Write(encoded); err != nil {
		file.Close()
		return fmt.Errorf("append journal file mutation: %w", err)
	}
	if err := file.Sync(); err != nil {
		file.Close()
		return fmt.Errorf("sync journal file mutation: %w", err)
	}
	if err := file.Close(); err != nil {
		return fmt.Errorf("close journal file mutation: %w", err)
	}
	return nil
}

func (s *Store) clearFileMutationsLocked() {
	if err := os.Remove(s.fileMutationPath()); err != nil && !errors.Is(err, fs.ErrNotExist) {
		return
	}
	if directory, err := os.Open(filepath.Dir(s.path)); err == nil {
		_ = directory.Sync()
		_ = directory.Close()
	}
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
	if err := encodeJournal(tmp, &data); err != nil {
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

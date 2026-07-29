package spoolreconcile

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"time"

	"starpilot.local/comma-companion-agent/internal/journal"
	"starpilot.local/comma-companion-agent/internal/state"
)

const DefaultMaxEntries = 10_000

type Logger interface {
	Printf(format string, values ...any)
}

type Result struct {
	Scanned              int   `json:"scanned"`
	Adopted              int   `json:"adopted"`
	TerminalLinksRemoved int   `json:"terminal_links_removed"`
	OrphansQuarantined   int   `json:"orphans_quarantined"`
	CleanupErrors        int   `json:"cleanup_errors"`
	QuarantineBytes      int64 `json:"quarantine_bytes"`
	Truncated            bool  `json:"truncated"`
}

type reconciler struct {
	spoolDir      string
	filesDir      string
	quarantineDir string
	store         *journal.Store
	logger        Logger
	now           time.Time
	maxEntries    int
	result        Result
	known         map[string]state.File
	seen          map[string]bool
	invalid       map[string]bool
}

func Reconcile(
	spoolDir string,
	store *journal.Store,
	logger Logger,
	maxEntries int,
) (Result, error) {
	if maxEntries <= 0 {
		maxEntries = DefaultMaxEntries
	}
	absoluteSpool, err := filepath.Abs(spoolDir)
	if err != nil {
		return Result{}, fmt.Errorf("resolve spool directory: %w", err)
	}
	r := &reconciler{
		spoolDir:      filepath.Clean(absoluteSpool),
		filesDir:      filepath.Join(absoluteSpool, "files"),
		quarantineDir: filepath.Join(absoluteSpool, "quarantine"),
		store:         store,
		logger:        logger,
		now:           time.Now().UTC(),
		maxEntries:    maxEntries,
		known:         make(map[string]state.File),
		seen:          make(map[string]bool),
		invalid:       make(map[string]bool),
	}
	if err := os.MkdirAll(r.filesDir, 0o700); err != nil {
		return Result{}, fmt.Errorf("create spool files directory: %w", err)
	}
	if err := os.MkdirAll(r.quarantineDir, 0o700); err != nil {
		return Result{}, fmt.Errorf("create spool quarantine directory: %w", err)
	}
	for _, file := range store.Snapshot().Files {
		path, pathErr := filepath.Abs(file.SpoolPath)
		if pathErr != nil || !within(r.filesDir, path) {
			r.invalid[file.ID] = true
			continue
		}
		r.known[filepath.Clean(path)] = file
	}

	stop := errors.New("bounded spool walk complete")
	err = filepath.WalkDir(r.filesDir, func(path string, entry fs.DirEntry, walkErr error) error {
		if walkErr != nil {
			r.result.CleanupErrors++
			r.logger.Printf("spool reconcile: walk %s: %v", path, walkErr)
			return nil
		}
		if path == r.filesDir {
			return nil
		}
		if entry.IsDir() {
			return nil
		}
		if r.result.Scanned >= r.maxEntries {
			r.result.Truncated = true
			return stop
		}
		r.result.Scanned++
		cleanPath := filepath.Clean(path)
		file, known := r.known[cleanPath]
		if !known {
			if quarantineErr := r.quarantine(cleanPath); quarantineErr != nil {
				r.result.CleanupErrors++
				r.logger.Printf("spool reconcile: quarantine %s: %v", cleanPath, quarantineErr)
			}
			return nil
		}
		r.seen[file.ID] = true
		if validationErr := validateKnownLink(file, cleanPath); validationErr != nil {
			r.invalid[file.ID] = true
			if quarantineErr := r.quarantine(cleanPath); quarantineErr != nil {
				r.result.CleanupErrors++
				r.logger.Printf(
					"spool reconcile: quarantine invalid known link %s: %v (validation: %v)",
					cleanPath,
					quarantineErr,
					validationErr,
				)
			}
			return nil
		}
		switch {
		case file.State == state.FileReleased && file.NeedsRespool:
			r.result.Adopted++
		case file.State == state.FileDurable,
			file.State == state.FileReleased,
			file.State == state.FileCancelPending &&
				(file.CancelNextState == state.FileReleased || file.CancelDeleteRecord):
			if removeErr := os.Remove(cleanPath); removeErr != nil && !errors.Is(removeErr, os.ErrNotExist) {
				r.result.CleanupErrors++
				r.logger.Printf("spool reconcile: remove terminal link %s: %v", cleanPath, removeErr)
			} else {
				r.result.TerminalLinksRemoved++
			}
		default:
			r.result.Adopted++
		}
		return nil
	})
	if err != nil && !errors.Is(err, stop) {
		return r.result, fmt.Errorf("walk spool files: %w", err)
	}
	if err := r.persist(); err != nil {
		return r.result, err
	}
	r.result.QuarantineBytes = directoryBytesBounded(r.quarantineDir, r.maxEntries)
	return r.result, nil
}

func (r *reconciler) persist() error {
	return r.store.Update(func(data *state.Journal) error {
		for id, file := range data.Files {
			if r.invalid[id] {
				// Never leave a journal-controlled cleanup path outside the
				// active spool tree. Later retry/durable cleanup must not be
				// able to unlink the recorded source or another arbitrary file.
				file.SpoolPath = safeMissingSpoolPath(r.filesDir, file.ID)
			}
			if file.State == state.FileReleased && file.NeedsRespool && r.seen[id] && !r.invalid[id] {
				file.State = state.FileRetry
				file.NeedsRespool = false
				file.ReleasedAt = time.Time{}
				file.LastError = ""
				data.Files[id] = file
				continue
			}
			if r.seen[id] && !r.invalid[id] {
				continue
			}
			if r.result.Truncated && !r.seen[id] && !r.invalid[id] {
				continue
			}
			if !r.invalid[id] {
				if _, err := os.Lstat(file.SpoolPath); err == nil {
					continue
				} else if !errors.Is(err, os.ErrNotExist) {
					data.Counters.SpoolCleanupErrors++
					continue
				}
			}
			handleMissing(data, id, file, r.now)
		}
		data.Counters.OrphansQuarantined += int64(r.result.OrphansQuarantined)
		data.Counters.SpoolCleanupErrors += int64(r.result.CleanupErrors)
		data.Counters.SpoolFilesReconciled += int64(
			r.result.Adopted + r.result.TerminalLinksRemoved,
		)
		return nil
	})
}

func safeMissingSpoolPath(filesDir, fileID string) string {
	if len(fileID) == 64 && strings.ToLower(fileID) == fileID {
		if decoded, err := hex.DecodeString(fileID); err == nil && len(decoded) == 32 {
			return filepath.Join(filesDir, fileID[:2], fileID[2:4], fileID+".data")
		}
	}
	digest := sha256.Sum256([]byte(fileID))
	name := hex.EncodeToString(digest[:])
	return filepath.Join(filesDir, "_invalid", name+".data")
}

func handleMissing(data *state.Journal, id string, file state.File, now time.Time) {
	sourceReady := false
	if info, err := os.Lstat(file.SourcePath); err == nil {
		sourceReady = info.Mode().IsRegular() &&
			info.Size() == file.Size &&
			(file.ModTimeNS == 0 || info.ModTime().UnixNano() == file.ModTimeNS)
	}
	switch file.State {
	case state.FileDurable, state.FileReleased:
		return
	case state.FileCancelPending:
		if file.CancelDeleteRecord {
			return
		}
		if file.CancelNextState == state.FileRetry {
			file.CancelNextState = state.FileReleased
			file.NeedsRespool = sourceReady
		} else if file.CancelNextState == state.FileCanceled {
			file.CancelNextState = state.FileReleased
		}
	default:
		file.NeedsRespool = sourceReady
		if file.UploadID != "" {
			file.State = state.FileCancelPending
			file.CancelRequestedAt = now
			file.CancelNextState = state.FileReleased
			queueCancellation(data, file.ID, file.UploadID, now)
		} else {
			file.State = state.FileReleased
			file.ReleasedAt = now
		}
	}
	file.LastError = "spool hardlink missing or invalid during startup reconciliation"
	data.Files[id] = file
}

func validateKnownLink(file state.File, path string) error {
	info, err := os.Lstat(path)
	if err != nil {
		return err
	}
	if info.Mode()&os.ModeSymlink != 0 || !info.Mode().IsRegular() {
		return errors.New("spool entry is not a regular non-symlink file")
	}
	if info.Size() != file.Size {
		return fmt.Errorf("spool size %d does not match journal size %d", info.Size(), file.Size)
	}
	if file.ModTimeNS != 0 && info.ModTime().UnixNano() != file.ModTimeNS {
		return errors.New("spool modification time does not match journal")
	}
	if source, sourceErr := os.Stat(file.SourcePath); sourceErr == nil {
		if !source.Mode().IsRegular() || !os.SameFile(source, info) {
			return errors.New("spool entry is not the recorded source hardlink")
		}
	}
	return nil
}

func (r *reconciler) quarantine(path string) error {
	info, err := os.Lstat(path)
	if err != nil {
		return err
	}
	relative, err := filepath.Rel(r.filesDir, path)
	if err != nil || strings.HasPrefix(relative, "..") {
		return errors.New("orphan path escaped spool files directory")
	}
	digest := sha256.Sum256([]byte(filepath.ToSlash(relative)))
	name := hex.EncodeToString(digest[:8]) + "-" + filepath.Base(path) + ".orphan"
	target := filepath.Join(r.quarantineDir, name)
	for suffix := 0; ; suffix++ {
		candidate := target
		if suffix > 0 {
			candidate = fmt.Sprintf("%s.%d", target, suffix)
		}
		if _, statErr := os.Lstat(candidate); errors.Is(statErr, os.ErrNotExist) {
			target = candidate
			break
		} else if statErr != nil {
			return statErr
		}
	}
	if err := os.Rename(path, target); err != nil {
		return err
	}
	r.result.OrphansQuarantined++
	if info.Mode().IsRegular() {
		r.result.QuarantineBytes += info.Size()
	}
	syncDirectory(filepath.Dir(path))
	syncDirectory(r.quarantineDir)
	return nil
}

func queueCancellation(data *state.Journal, fileID, uploadID string, now time.Time) {
	if fileID == "" || uploadID == "" {
		return
	}
	key := state.CancellationKey(fileID, uploadID)
	if _, exists := data.Cancellations[key]; exists {
		return
	}
	data.Cancellations[key] = state.UploadCancellation{
		FileID:      fileID,
		UploadID:    uploadID,
		RequestedAt: now,
	}
}

func within(root, path string) bool {
	relative, err := filepath.Rel(root, path)
	return err == nil && relative != ".." &&
		!strings.HasPrefix(relative, ".."+string(filepath.Separator))
}

func directoryBytesBounded(root string, maxEntries int) int64 {
	var total int64
	seen := 0
	_ = filepath.WalkDir(root, func(path string, entry fs.DirEntry, walkErr error) error {
		if walkErr != nil || entry.IsDir() {
			return nil
		}
		if seen >= maxEntries {
			return fs.SkipAll
		}
		seen++
		if info, err := entry.Info(); err == nil && info.Mode().IsRegular() {
			total += info.Size()
		}
		return nil
	})
	return total
}

func syncDirectory(path string) {
	directory, err := os.Open(path)
	if err != nil {
		return
	}
	_ = directory.Sync()
	_ = directory.Close()
}

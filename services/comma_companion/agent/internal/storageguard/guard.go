package storageguard

import (
	"errors"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"

	"starpilot.local/comma-companion-agent/internal/config"
	"starpilot.local/comma-companion-agent/internal/journal"
	"starpilot.local/comma-companion-agent/internal/state"
)

type Logger interface {
	Printf(format string, values ...any)
}

type Status struct {
	RetainedBytes       int64     `json:"retained_bytes"`
	FreeBytes           int64     `json:"free_bytes"`
	AllowNew            bool      `json:"allow_new"`
	Pressure            bool      `json:"pressure"`
	Reason              string    `json:"reason,omitempty"`
	ReleasedFiles       int       `json:"released_files"`
	QuarantineBytes     int64     `json:"quarantine_bytes"`
	QuarantineFiles     int       `json:"quarantine_files"`
	QuarantineTruncated bool      `json:"quarantine_truncated"`
	CheckedAt           time.Time `json:"checked_at"`
}

type Guard struct {
	config  config.Storage
	spool   string
	journal *journal.Store
	logger  Logger
	mu      sync.RWMutex
	status  Status
}

type retainedFile struct {
	file state.File
	size int64
}

func New(cfg config.Storage, spool string, store *journal.Store, logger Logger) *Guard {
	return &Guard{config: cfg, spool: spool, journal: store, logger: logger}
}

func (g *Guard) Snapshot() Status {
	g.mu.RLock()
	defer g.mu.RUnlock()
	return g.status
}

func (g *Guard) Enforce() Status {
	now := time.Now().UTC()
	snapshot := g.journal.Snapshot()
	retained, candidates := retainedFiles(snapshot, filepath.Join(g.spool, "files"))
	quarantineBytes, quarantineFiles, quarantineTruncated, quarantineErr := quarantineUsage(
		filepath.Join(g.spool, "quarantine"),
		10_000,
	)
	retained += quarantineBytes
	free, err := freeBytes(g.spool)
	status := Status{
		RetainedBytes:       retained,
		FreeBytes:           free,
		AllowNew:            true,
		QuarantineBytes:     quarantineBytes,
		QuarantineFiles:     quarantineFiles,
		QuarantineTruncated: quarantineTruncated,
		CheckedAt:           now,
	}
	if quarantineErr != nil {
		status.AllowNew = false
		status.Pressure = true
		status.Reason = "cannot inspect quarantined spool entries"
		g.set(status)
		g.logger.Printf("storage: inspect quarantine: %v", quarantineErr)
		return status
	}
	if err != nil {
		status.AllowNew = false
		status.Pressure = true
		status.Reason = "cannot determine free space"
		g.set(status)
		g.logger.Printf("storage: cannot determine free space: %v", err)
		return status
	}
	if retained <= g.config.MaxRetainedBytes && free >= g.config.MinFreeBytes &&
		!quarantineTruncated {
		g.set(status)
		return status
	}
	status.Pressure = true
	if quarantineTruncated {
		status.Reason = "quarantine entry scan limit reached; manual review required"
	} else if retained > g.config.MaxRetainedBytes {
		status.Reason = "retained spool limit exceeded"
	} else {
		status.Reason = "device free-space floor reached"
	}
	if g.config.EmergencyBehavior == "pause" {
		status.AllowNew = false
		g.set(status)
		return status
	}

	sort.Slice(candidates, func(i, j int) bool {
		leftPriority := releasePriority(candidates[i].file.State)
		rightPriority := releasePriority(candidates[j].file.State)
		if leftPriority != rightPriority {
			return leftPriority < rightPriority
		}
		return candidates[i].file.SpooledAt.Before(candidates[j].file.SpooledAt)
	})
	for _, candidate := range candidates {
		if retained <= g.config.MaxRetainedBytes && free >= g.config.MinFreeBytes {
			break
		}
		file := candidate.file
		releasedAt := time.Now().UTC()
		countRelease := false
		if err := g.journal.Update(func(data *state.Journal) error {
			current, ok := data.Files[file.ID]
			if !ok {
				return nil
			}
			needsRespool := releaseNeedsRespool(current)
			releaseReason := "spool hardlink release requested by local storage safety policy"
			if releaseCanRetry(current) {
				if needsRespool {
					releaseReason += "; exact unchanged source retained for retry"
				} else {
					releaseReason += "; source is missing or changed and cannot be respooled"
				}
			}
			switch current.State {
			case state.FileDurable:
				// The terminal state was durably recorded before unlink. Retrying
				// the unlink is safe and keeps crash recovery monotonic.
				current.NeedsRespool = false
			case state.FileReleased:
				// A released record is only retryable when an earlier storage
				// release already marked it and the exact source still exists.
				// Never resurrect an intentional terminal release here.
				current.NeedsRespool = needsRespool
			case state.FileCancelPending:
				if current.CancelNextState != state.FileReleased &&
					!current.CancelDeleteRecord {
					current.CancelNextState = state.FileReleased
					current.NeedsRespool = needsRespool
					current.ReleasedAt = releasedAt
					current.LastError = releaseReason
					data.Counters.FilesReleased++
					countRelease = true
				} else if current.CancelNextState == state.FileReleased {
					current.NeedsRespool = needsRespool
					current.LastError = releaseReason
				}
			default:
				if current.UploadID != "" {
					current.State = state.FileCancelPending
					current.CancelRequestedAt = releasedAt
					current.CancelNextState = state.FileReleased
					current.CancelDeleteRecord = false
					queueCancellation(data, current.ID, current.UploadID, releasedAt)
				} else {
					current.State = state.FileReleased
				}
				current.NeedsRespool = needsRespool
				current.ReleasedAt = releasedAt
				current.LastError = releaseReason
				data.Counters.FilesReleased++
				countRelease = true
			}
			data.Files[file.ID] = current
			return nil
		}); err != nil {
			g.logger.Printf("storage: record emergency release %s: %v", file.ID, err)
			continue
		}
		if err := os.Remove(file.SpoolPath); err != nil && !errors.Is(err, os.ErrNotExist) {
			g.logger.Printf("storage: emergency release %s: %v", file.SpoolPath, err)
			continue
		}
		retained -= candidate.size
		if countRelease {
			status.ReleasedFiles++
		}
		if latestFree, err := freeBytes(g.spool); err == nil {
			free = latestFree
		}
	}
	status.RetainedBytes = retained
	status.FreeBytes = free
	status.AllowNew = retained <= g.config.MaxRetainedBytes &&
		free >= g.config.MinFreeBytes &&
		!quarantineTruncated
	if !status.AllowNew && len(candidates) == status.ReleasedFiles {
		status.Reason += "; no protected files remain to release"
	}
	g.set(status)
	if status.ReleasedFiles > 0 {
		g.logger.Printf(
			"storage: emergency release files=%d retained_bytes=%d free_bytes=%d",
			status.ReleasedFiles,
			status.RetainedBytes,
			status.FreeBytes,
		)
	}
	return status
}

func quarantineUsage(root string, limit int) (int64, int, bool, error) {
	if limit <= 0 {
		limit = 10_000
	}
	var bytes int64
	files := 0
	truncated := false
	err := filepath.WalkDir(root, func(path string, entry fs.DirEntry, walkErr error) error {
		if walkErr != nil {
			if errors.Is(walkErr, os.ErrNotExist) && path == root {
				return fs.SkipAll
			}
			return walkErr
		}
		if entry.IsDir() {
			return nil
		}
		if files >= limit {
			truncated = true
			return fs.SkipAll
		}
		files++
		info, err := entry.Info()
		if err != nil {
			return err
		}
		if info.Mode().IsRegular() {
			bytes += info.Size()
		}
		return nil
	})
	if errors.Is(err, os.ErrNotExist) {
		err = nil
	}
	return bytes, files, truncated, err
}

func (g *Guard) set(status Status) {
	g.mu.Lock()
	g.status = status
	g.mu.Unlock()
}

func retainedFiles(snapshot state.Journal, filesRoot string) (int64, []retainedFile) {
	var retained int64
	files := make([]retainedFile, 0)
	for _, file := range snapshot.Files {
		if !pathWithin(filesRoot, file.SpoolPath) {
			continue
		}
		info, err := os.Lstat(file.SpoolPath)
		if errors.Is(err, os.ErrNotExist) {
			continue
		}
		size := file.Size
		if err == nil && info.Mode().IsRegular() {
			size = info.Size()
		}
		retained += size
		files = append(files, retainedFile{file: file, size: size})
	}
	return retained, files
}

func pathWithin(root, path string) bool {
	rootAbsolute, rootErr := filepath.Abs(root)
	pathAbsolute, pathErr := filepath.Abs(path)
	if rootErr != nil || pathErr != nil {
		return false
	}
	relative, err := filepath.Rel(rootAbsolute, pathAbsolute)
	return err == nil && relative != ".." &&
		!filepath.IsAbs(relative) &&
		!strings.HasPrefix(relative, ".."+string(filepath.Separator))
}

func releasePriority(fileState state.FileState) int {
	switch fileState {
	case state.FileDurable, state.FileReleased:
		return 0
	case state.FileCanceled:
		return 1
	case state.FileFailed:
		return 2
	case state.FileRetry:
		return 3
	case state.FileSpooled:
		return 4
	case state.FileUploading:
		return 5
	case state.FileCancelPending:
		return 6
	default:
		return 7
	}
}

func releaseNeedsRespool(file state.File) bool {
	if !file.DurableAt.IsZero() {
		return false
	}
	switch file.State {
	case state.FileSpooled, state.FileUploading, state.FileRetry:
		return sourceMatchesSpoolLink(file)
	case state.FileCancelPending:
		if file.CancelDeleteRecord {
			return false
		}
		switch file.CancelNextState {
		case state.FileRetry:
			return sourceMatchesSpoolLink(file)
		case state.FileReleased:
			return file.NeedsRespool && sourceMatchesSpoolLink(file)
		default:
			return false
		}
	case state.FileReleased:
		return file.NeedsRespool && sourceMatchesSpoolLink(file)
	default:
		return false
	}
}

func releaseCanRetry(file state.File) bool {
	if !file.DurableAt.IsZero() {
		return false
	}
	switch file.State {
	case state.FileSpooled, state.FileUploading, state.FileRetry:
		return true
	case state.FileCancelPending:
		return !file.CancelDeleteRecord &&
			(file.CancelNextState == state.FileRetry ||
				(file.CancelNextState == state.FileReleased && file.NeedsRespool))
	case state.FileReleased:
		return file.NeedsRespool
	default:
		return false
	}
}

func sourceMatchesSpoolLink(file state.File) bool {
	if file.SourcePath == "" || file.SpoolPath == "" {
		return false
	}
	source, sourceErr := os.Lstat(file.SourcePath)
	spool, spoolErr := os.Lstat(file.SpoolPath)
	if sourceErr != nil || spoolErr != nil ||
		source.Mode()&os.ModeSymlink != 0 || spool.Mode()&os.ModeSymlink != 0 ||
		!source.Mode().IsRegular() || !spool.Mode().IsRegular() ||
		source.Size() != file.Size || spool.Size() != file.Size ||
		(file.ModTimeNS != 0 &&
			(source.ModTime().UnixNano() != file.ModTimeNS ||
				spool.ModTime().UnixNano() != file.ModTimeNS)) {
		return false
	}
	return os.SameFile(source, spool)
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

package uploader

import (
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"sort"
	"strings"
	"sync"
	"time"

	"starpilot.local/comma-companion-agent/internal/api"
	"starpilot.local/comma-companion-agent/internal/journal"
	"starpilot.local/comma-companion-agent/internal/policy"
	"starpilot.local/comma-companion-agent/internal/state"
)

type Logger interface {
	Printf(format string, values ...any)
}

var errUploadStopped = errors.New("upload was stopped by local state change")
var errManualRetryRequired = errors.New("automatic upload redeclaration limit reached; manual retry required")

type Metrics struct {
	ActiveFile      string    `json:"active_file,omitempty"`
	ActiveOffset    int64     `json:"active_offset"`
	SessionBytes    int64     `json:"session_bytes"`
	UploadBytesPerS float64   `json:"upload_bytes_per_second"`
	LastSuccessAt   time.Time `json:"last_success_at,omitempty"`
	LastError       string    `json:"last_error,omitempty"`
}

type Uploader struct {
	client    *api.Client
	journal   *journal.Store
	policy    *policy.Reader
	chunkSize int64
	logger    Logger
	now       func() time.Time
	startedAt time.Time
	mu        sync.RWMutex
	metrics   Metrics
	pickCount int
}

func New(client *api.Client, store *journal.Store, policyReader *policy.Reader, chunkSize int64, logger Logger) *Uploader {
	now := time.Now().UTC()
	return &Uploader{
		client:    client,
		journal:   store,
		policy:    policyReader,
		chunkSize: chunkSize,
		logger:    logger,
		now:       time.Now,
		startedAt: now,
	}
}

func (u *Uploader) SnapshotMetrics() Metrics {
	u.mu.RLock()
	defer u.mu.RUnlock()
	result := u.metrics
	elapsed := time.Since(u.startedAt).Seconds()
	if elapsed > 0 {
		result.UploadBytesPerS = float64(result.SessionBytes) / elapsed
	}
	return result
}

func (u *Uploader) ProcessOnce(ctx context.Context) (bool, error) {
	snapshot := u.journal.View()
	now := u.now().UTC()
	if cancellation, found := nextCancellation(snapshot, now); found {
		u.setActive(cancellation.FileID, 0)
		defer u.setActive("", 0)
		if err := u.cancel(ctx, cancellation); err != nil {
			u.recordCancelFailure(state.CancellationKey(cancellation.FileID, cancellation.UploadID), err)
			return true, err
		}
		return true, nil
	}
	if routeInventory, found := nextRouteInventory(snapshot, now); found {
		if _, err := u.client.DeclareRouteInventory(ctx, routeInventory); err != nil {
			u.recordInventoryFailure(routeInventory.ManifestSHA256, err)
			return true, fmt.Errorf(
				"declare route inventory %s generation %d: %w",
				routeInventory.Manifest.RouteName,
				routeInventory.Manifest.Generation,
				err,
			)
		}
		if err := u.finishInventoryDeclaration(routeInventory.ManifestSHA256); err != nil {
			return true, fmt.Errorf("persist route inventory acceptance: %w", err)
		}
		return true, nil
	}
	if status := u.policy.Read(); !status.UploadAllowed {
		return false, nil
	}
	if snapshot.Paused {
		return false, nil
	}
	u.mu.Lock()
	u.pickCount++
	fairPick := u.pickCount%16 == 0
	u.mu.Unlock()
	file, found := nextFile(snapshot, now, fairPick)
	if !found {
		return false, nil
	}
	u.setActive(file.ID, file.UploadOffset)
	defer u.setActive("", 0)
	if err := u.upload(ctx, file); err != nil {
		u.recordFailure(file.ID, err)
		return true, err
	}
	return true, nil
}

func nextCancellation(snapshot state.Journal, now time.Time) (state.UploadCancellation, bool) {
	cancellations := make([]state.UploadCancellation, 0, len(snapshot.Cancellations))
	for _, cancellation := range snapshot.Cancellations {
		if cancellation.NextAttemptAt.IsZero() || !now.Before(cancellation.NextAttemptAt) {
			cancellations = append(cancellations, cancellation)
		}
	}
	if len(cancellations) == 0 {
		return state.UploadCancellation{}, false
	}
	sort.Slice(cancellations, func(i, j int) bool {
		if cancellations[i].RequestedAt.Equal(cancellations[j].RequestedAt) {
			if cancellations[i].FileID == cancellations[j].FileID {
				return cancellations[i].UploadID < cancellations[j].UploadID
			}
			return cancellations[i].FileID < cancellations[j].FileID
		}
		return cancellations[i].RequestedAt.Before(cancellations[j].RequestedAt)
	})
	return cancellations[0], true
}

func nextRouteInventory(snapshot state.Journal, now time.Time) (state.RouteInventory, bool) {
	inventories := make([]state.RouteInventory, 0, len(snapshot.Inventories))
	for _, inventory := range snapshot.Inventories {
		if !inventory.DeclaredAt.IsZero() ||
			(!inventory.NextAttemptAt.IsZero() && now.Before(inventory.NextAttemptAt)) {
			continue
		}
		priorPending := false
		for _, prior := range snapshot.Inventories {
			if prior.Manifest.RouteName == inventory.Manifest.RouteName &&
				prior.Manifest.Generation < inventory.Manifest.Generation &&
				prior.DeclaredAt.IsZero() {
				priorPending = true
				break
			}
		}
		if !priorPending {
			inventories = append(inventories, inventory)
		}
	}
	if len(inventories) == 0 {
		return state.RouteInventory{}, false
	}
	sort.Slice(inventories, func(i, j int) bool {
		if inventories[i].Manifest.RouteName == inventories[j].Manifest.RouteName {
			return inventories[i].Manifest.Generation < inventories[j].Manifest.Generation
		}
		return inventories[i].Manifest.RouteName < inventories[j].Manifest.RouteName
	})
	return inventories[0], true
}

func nextFile(snapshot state.Journal, now time.Time, fairPick bool) (state.File, bool) {
	files := make([]state.File, 0)
	for _, file := range snapshot.Files {
		switch file.State {
		case state.FileSpooled, state.FileUploading, state.FileRetry:
			if file.NextAttemptAt.IsZero() || !now.Before(file.NextAttemptAt) {
				files = append(files, file)
			}
		}
	}
	if len(files) == 0 {
		return state.File{}, false
	}
	sort.Slice(files, func(i, j int) bool {
		if !fairPick {
			leftPriority := uploadPriority(files[i])
			rightPriority := uploadPriority(files[j])
			if leftPriority != rightPriority {
				return leftPriority < rightPriority
			}
		}
		if files[i].SpooledAt.Equal(files[j].SpooledAt) {
			return files[i].RelativePath < files[j].RelativePath
		}
		return files[i].SpooledAt.Before(files[j].SpooledAt)
	})
	return files[0], true
}

func uploadPriority(file state.File) int {
	switch {
	case file.ArtifactType == "qlog":
		return 0
	case file.ArtifactType == "rlog":
		return 1
	case file.Camera == "qcamera":
		return 2
	case file.Camera == "road":
		return 3
	case file.Camera == "wide":
		return 4
	case file.Camera == "driver":
		return 5
	default:
		return 6
	}
}

func (u *Uploader) upload(ctx context.Context, file state.File) error {
	info, err := os.Stat(file.SpoolPath)
	if err != nil {
		return fmt.Errorf("stat spool file: %w", err)
	}
	if info.Size() != file.Size || (file.ModTimeNS != 0 && info.ModTime().UnixNano() != file.ModTimeNS) {
		if err := u.invalidate(file); err != nil {
			return fmt.Errorf("invalidate changed spool file: %w", err)
		}
		return errors.New("spool file changed after hashing; returned to stable observation")
	}

	var status api.UploadStatus
	if file.UploadID == "" {
		status, err = u.client.CreateUpload(ctx, file)
		if err != nil {
			return fmt.Errorf("create upload: %w", err)
		}
		if status.ID == "" {
			return errors.New("create upload response lacks upload ID")
		}
		if status.NeedsRedeclare() {
			if err := u.scheduleAutomaticRedeclare(
				file.ID,
				file.UploadAttempt,
				"",
				status.ID,
				status.State,
				false,
			); err != nil {
				return err
			}
			return fmt.Errorf("declared upload returned terminal state %s; redeclaration attempt advanced", status.State)
		}
		if status.Length != 0 && status.Length != file.Size {
			return fmt.Errorf("server upload length %d does not match %d", status.Length, file.Size)
		}
		if status.Offset < 0 || status.Offset > file.Size {
			return fmt.Errorf("server offset %d is outside file length %d", status.Offset, file.Size)
		}
		file.UploadID = status.ID
		if err := u.bindUpload(file.ID, file.UploadAttempt, status.ID, status.Offset); err != nil {
			return err
		}
	} else {
		status, err = u.client.HeadUpload(ctx, file.UploadID)
		if errors.Is(err, api.ErrNotFound) {
			if err := u.scheduleAutomaticRedeclare(
				file.ID,
				file.UploadAttempt,
				file.UploadID,
				"",
				"not_found",
				true,
			); err != nil {
				return err
			}
			return errors.New("server forgot upload; declaration generation advanced")
		}
		if err != nil {
			return fmt.Errorf("inspect upload: %w", err)
		}
		if status.NeedsRedeclare() {
			if err := u.scheduleAutomaticRedeclare(
				file.ID,
				file.UploadAttempt,
				file.UploadID,
				file.UploadID,
				status.State,
				false,
			); err != nil {
				return err
			}
			return fmt.Errorf("upload session is %s; redeclaration attempt advanced", status.State)
		}
	}
	if status.Offset < 0 || status.Offset > file.Size {
		return fmt.Errorf("server offset %d is outside file length %d", status.Offset, file.Size)
	}
	if status.Length != 0 && status.Length != file.Size {
		return fmt.Errorf("server upload length %d does not match %d", status.Length, file.Size)
	}
	if status.Durable {
		return u.finishDurable(file, status)
	}
	if status.Offset == file.Size {
		return errors.New("server has all bytes but has not acknowledged durable verification")
	}

	handle, err := os.Open(file.SpoolPath)
	if err != nil {
		return err
	}
	remaining := file.Size - status.Offset
	readSize := u.chunkSize
	if remaining < readSize {
		readSize = remaining
	}
	chunk := make([]byte, readSize)
	read, err := handle.ReadAt(chunk, status.Offset)
	closeErr := handle.Close()
	if err != nil && !errors.Is(err, io.EOF) {
		return fmt.Errorf("read upload chunk: %w", err)
	}
	if closeErr != nil {
		return fmt.Errorf("close upload chunk: %w", closeErr)
	}
	chunk = chunk[:read]
	if len(chunk) == 0 {
		return errors.New("spool file ended before declared length")
	}
	if latestPolicy := u.policy.Read(); !latestPolicy.UploadAllowed {
		return nil
	}
	next, err := u.client.PatchUpload(ctx, file.UploadID, status.Offset, file.Size, chunk)
	if err != nil {
		return fmt.Errorf("upload chunk: %w", err)
	}
	if next.NeedsRedeclare() {
		if err := u.scheduleAutomaticRedeclare(
			file.ID,
			file.UploadAttempt,
			file.UploadID,
			file.UploadID,
			next.State,
			false,
		); err != nil {
			return err
		}
		return fmt.Errorf("upload session became %s; redeclaration attempt advanced", next.State)
	}
	expectedOffset := status.Offset + int64(len(chunk))
	if next.Offset != expectedOffset {
		return fmt.Errorf("server advanced to offset %d, expected %d", next.Offset, expectedOffset)
	}
	if next.SHA256 != "" && next.Offset == file.Size && !strings.EqualFold(next.SHA256, file.SHA256) {
		return fmt.Errorf("server SHA-256 %s does not match %s", next.SHA256, file.SHA256)
	}
	if err := u.updateChunk(file.ID, next.Offset, int64(len(chunk))); err != nil {
		return err
	}
	u.addBytes(int64(len(chunk)))
	if next.Durable {
		file.UploadOffset = next.Offset
		return u.finishDurable(file, next)
	}
	return nil
}

func (u *Uploader) cancel(ctx context.Context, cancellation state.UploadCancellation) error {
	file := state.File{ID: cancellation.FileID, UploadID: cancellation.UploadID}
	status, err := u.client.CancelUpload(ctx, file)
	if errors.Is(err, api.ErrNotFound) {
		return u.completeCancellation(cancellation)
	}
	if err != nil {
		if head, headErr := u.client.HeadUpload(ctx, cancellation.UploadID); headErr == nil {
			if head.Durable {
				snapshot := u.journal.View()
				if current, exists := snapshot.Files[cancellation.FileID]; exists {
					if durableErr := u.finishDurable(current, head); durableErr != nil {
						return durableErr
					}
				}
				return u.removeCancellation(cancellation)
			}
			if head.IsCanceled() {
				return u.completeCancellation(cancellation)
			}
		}
		return fmt.Errorf("abandon server upload: %w", err)
	}
	if !status.IsCanceled() {
		return fmt.Errorf("server cancel returned state %q", status.State)
	}
	return u.completeCancellation(cancellation)
}

func (u *Uploader) completeCancellation(cancellation state.UploadCancellation) error {
	now := u.now().UTC()
	snapshot := u.journal.View()
	if file, exists := snapshot.Files[cancellation.FileID]; exists &&
		file.State == state.FileCancelPending &&
		file.UploadID == cancellation.UploadID &&
		file.CancelDeleteRecord {
		if err := os.Remove(file.SpoolPath); err != nil && !errors.Is(err, os.ErrNotExist) {
			return fmt.Errorf("remove invalidated spool before forgetting it: %w", err)
		}
	}
	return u.journal.Update(func(data *state.Journal) error {
		key := state.CancellationKey(cancellation.FileID, cancellation.UploadID)
		if _, exists := data.Cancellations[key]; !exists {
			return nil
		}
		delete(data.Cancellations, key)
		file, ok := data.Files[cancellation.FileID]
		if !ok || file.State != state.FileCancelPending || file.UploadID != cancellation.UploadID {
			return nil
		}
		if file.CancelDeleteRecord {
			delete(data.Files, cancellation.FileID)
			delete(data.Observations, file.SourcePath)
			return nil
		}
		nextState := file.CancelNextState
		if nextState == "" {
			nextState = state.FileCanceled
		}
		file.State = nextState
		file.UploadID = ""
		file.UploadOffset = 0
		file.CancelRequestedAt = time.Time{}
		file.CancelNextState = ""
		file.CancelDeleteRecord = false
		file.Attempts = 0
		file.NextAttemptAt = time.Time{}
		file.LastError = ""
		if nextState == state.FileReleased && file.ReleasedAt.IsZero() {
			file.ReleasedAt = now
		}
		data.Files[cancellation.FileID] = file
		return nil
	})
}

func (u *Uploader) removeCancellation(cancellation state.UploadCancellation) error {
	return u.journal.Update(func(data *state.Journal) error {
		delete(data.Cancellations, state.CancellationKey(cancellation.FileID, cancellation.UploadID))
		return nil
	})
}

func (u *Uploader) scheduleAutomaticRedeclare(
	fileID string,
	expectedAttempt int,
	expectedUploadID string,
	serverUploadID string,
	terminalState string,
	serverGone bool,
) error {
	now := u.now().UTC()
	retryLimitReached := false
	err := u.journal.Update(func(data *state.Journal) error {
		file, ok := data.Files[fileID]
		if !ok {
			return errors.New("file disappeared from journal")
		}
		if file.State == state.FileCancelPending || file.State == state.FileCanceled ||
			file.State == state.FileDurable || file.State == state.FileReleased {
			return errUploadStopped
		}
		if file.UploadAttempt != expectedAttempt || file.UploadID != expectedUploadID {
			return errUploadStopped
		}
		if file.AutoRedeclarations >= 1 {
			file.LastError = "server upload became " + terminalState +
				"; automatic redeclaration limit reached, manual retry required"
			file.NextAttemptAt = time.Time{}
			if serverGone || serverUploadID == "" {
				file.UploadID = ""
				file.UploadOffset = 0
				file.State = state.FileFailed
			} else {
				file.UploadID = serverUploadID
				file.State = state.FileCancelPending
				file.CancelRequestedAt = now
				file.CancelNextState = state.FileFailed
				file.CancelDeleteRecord = false
				queueCancellation(data, file.ID, serverUploadID, now)
			}
			data.Files[fileID] = file
			retryLimitReached = true
			return nil
		}
		file.UploadAttempt++
		file.AutoRedeclarations++
		file.Attempts = 0
		file.NextAttemptAt = time.Time{}
		file.LastError = "server upload became " + terminalState + "; redeclaration required"
		if serverGone || serverUploadID == "" {
			file.UploadID = ""
			file.UploadOffset = 0
			file.State = state.FileRetry
			file.CancelRequestedAt = time.Time{}
			file.CancelNextState = ""
			file.CancelDeleteRecord = false
		} else {
			file.UploadID = serverUploadID
			file.State = state.FileCancelPending
			file.CancelRequestedAt = now
			file.CancelNextState = state.FileRetry
			file.CancelDeleteRecord = false
			queueCancellation(data, file.ID, serverUploadID, now)
		}
		data.Files[fileID] = file
		return nil
	})
	if err != nil {
		return err
	}
	if retryLimitReached {
		return errManualRetryRequired
	}
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

func (u *Uploader) invalidate(file state.File) error {
	now := u.now().UTC()
	hasServerUpload := false
	if err := u.journal.Update(func(data *state.Journal) error {
		current, ok := data.Files[file.ID]
		if !ok {
			return nil
		}
		if current.State == state.FileDurable {
			return nil
		}
		current.CancelDeleteRecord = true
		current.NeedsRespool = false
		current.LastError = "spool changed after hashing; server upload cleanup required"
		if current.UploadID != "" {
			hasServerUpload = true
			current.State = state.FileCancelPending
			current.CancelRequestedAt = now
			current.CancelNextState = ""
			queueCancellation(data, current.ID, current.UploadID, now)
		} else {
			current.State = state.FileReleased
			current.ReleasedAt = now
		}
		data.Files[file.ID] = current
		return nil
	}); err != nil {
		return err
	}
	if err := os.Remove(file.SpoolPath); err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	if hasServerUpload {
		return nil
	}
	return u.journal.Update(func(data *state.Journal) error {
		current, ok := data.Files[file.ID]
		if !ok {
			return nil
		}
		if current.State != state.FileReleased || !current.CancelDeleteRecord {
			return errUploadStopped
		}
		delete(data.Files, file.ID)
		delete(data.Observations, current.SourcePath)
		return nil
	})
}

func (u *Uploader) finishDurable(file state.File, status api.UploadStatus) error {
	if status.Offset != file.Size {
		return fmt.Errorf("durable upload offset %d does not match file length %d", status.Offset, file.Size)
	}
	if status.Length != file.Size {
		return fmt.Errorf("durable upload length %d does not match file length %d", status.Length, file.Size)
	}
	if status.SHA256 == "" {
		return errors.New("durable upload acknowledgement lacks server SHA-256")
	}
	if !strings.EqualFold(status.SHA256, file.SHA256) {
		return fmt.Errorf("durable upload SHA-256 %s does not match %s", status.SHA256, file.SHA256)
	}
	now := u.now().UTC()
	newlyDurable := false
	if err := u.journal.UpdateFile(file.ID, func(
		current *state.File,
		counters *state.Counters,
	) (bool, error) {
		if current.State == state.FileDurable {
			return false, nil
		}
		current.State = state.FileDurable
		current.UploadOffset = current.Size
		current.LastError = ""
		current.DurableAt = now
		current.NextAttemptAt = time.Time{}
		current.CancelRequestedAt = time.Time{}
		current.CancelNextState = ""
		current.CancelDeleteRecord = false
		current.NeedsRespool = false
		counters.FilesDurable++
		newlyDurable = true
		return true, nil
	}); err != nil {
		return err
	}
	if err := os.Remove(file.SpoolPath); err != nil && !errors.Is(err, os.ErrNotExist) {
		u.logger.Printf("upload: durable spool cleanup %s: %v", file.SpoolPath, err)
	}
	if newlyDurable {
		u.mu.Lock()
		u.metrics.LastSuccessAt = now
		u.metrics.LastError = ""
		u.mu.Unlock()
	}
	return nil
}

func (u *Uploader) bindUpload(fileID string, expectedAttempt int, uploadID string, offset int64) error {
	err := u.journal.UpdateFile(fileID, func(
		file *state.File,
		_ *state.Counters,
	) (bool, error) {
		if file.UploadAttempt != expectedAttempt ||
			file.State == state.FileReleased ||
			file.State == state.FileCancelPending ||
			file.State == state.FileCanceled ||
			file.State == state.FileDurable ||
			file.State == state.FileFailed {
			return false, errUploadStopped
		}
		file.UploadID = uploadID
		file.UploadOffset = offset
		file.State = state.FileUploading
		file.LastError = ""
		file.NextAttemptAt = time.Time{}
		return true, nil
	})
	if err == nil {
		return nil
	}
	if !errors.Is(err, errUploadStopped) && !errors.Is(err, journal.ErrFileNotFound) {
		return err
	}
	return u.bindUploadFallback(fileID, expectedAttempt, uploadID, offset)
}

func (u *Uploader) bindUploadFallback(fileID string, expectedAttempt int, uploadID string, offset int64) error {
	stopped := false
	now := u.now().UTC()
	err := u.journal.Update(func(data *state.Journal) error {
		file, ok := data.Files[fileID]
		if !ok {
			queueCancellation(data, fileID, uploadID, now)
			stopped = true
			return nil
		}
		if file.UploadAttempt != expectedAttempt ||
			file.State == state.FileReleased ||
			file.State == state.FileCancelPending ||
			file.State == state.FileCanceled ||
			file.State == state.FileDurable ||
			file.State == state.FileFailed {
			queueCancellation(data, fileID, uploadID, now)
			if file.UploadID == "" &&
				(file.State == state.FileCanceled || file.State == state.FileCancelPending) {
				file.UploadID = uploadID
				file.State = state.FileCancelPending
				file.CancelRequestedAt = now
				if file.CancelNextState == "" {
					file.CancelNextState = state.FileCanceled
				}
				data.Files[fileID] = file
			}
			stopped = true
			return nil
		}
		file.UploadID = uploadID
		file.UploadOffset = offset
		file.State = state.FileUploading
		file.LastError = ""
		file.NextAttemptAt = time.Time{}
		data.Files[fileID] = file
		return nil
	})
	if err != nil {
		return err
	}
	if stopped {
		return errUploadStopped
	}
	return nil
}

func (u *Uploader) updateProgress(fileID, uploadID string, offset int64, fileState state.FileState) error {
	return u.journal.UpdateFile(fileID, func(
		file *state.File,
		_ *state.Counters,
	) (bool, error) {
		if file.State == state.FileReleased || file.State == state.FileCancelPending ||
			file.State == state.FileCanceled || file.State == state.FileDurable {
			return false, errUploadStopped
		}
		file.UploadID = uploadID
		file.UploadOffset = offset
		file.State = fileState
		file.LastError = ""
		file.NextAttemptAt = time.Time{}
		return true, nil
	})
}

func (u *Uploader) updateChunk(fileID string, offset, bytes int64) error {
	return u.journal.UpdateFile(fileID, func(
		file *state.File,
		counters *state.Counters,
	) (bool, error) {
		if file.State == state.FileReleased || file.State == state.FileCancelPending ||
			file.State == state.FileCanceled || file.State == state.FileDurable {
			return false, errUploadStopped
		}
		file.UploadOffset = offset
		file.State = state.FileUploading
		file.LastError = ""
		file.NextAttemptAt = time.Time{}
		counters.BytesUploaded += bytes
		return true, nil
	})
}

func (u *Uploader) recordFailure(fileID string, uploadError error) {
	now := u.now().UTC()
	_ = u.journal.UpdateFile(fileID, func(
		file *state.File,
		counters *state.Counters,
	) (bool, error) {
		if file.State == state.FileReleased || file.State == state.FileCancelPending ||
			file.State == state.FileDurable || file.State == state.FileCanceled ||
			file.State == state.FileFailed {
			return false, nil
		}
		file.Attempts++
		file.State = state.FileRetry
		file.LastError = uploadError.Error()
		file.NextAttemptAt = now.Add(backoff(file.Attempts))
		counters.UploadErrors++
		return true, nil
	})
	u.mu.Lock()
	u.metrics.LastError = uploadError.Error()
	u.mu.Unlock()
}

func (u *Uploader) recordCancelFailure(cancellationKey string, cancelError error) {
	now := u.now().UTC()
	_ = u.journal.Update(func(data *state.Journal) error {
		cancellation, ok := data.Cancellations[cancellationKey]
		if !ok {
			return nil
		}
		cancellation.Attempts++
		cancellation.LastError = cancelError.Error()
		cancellation.NextAttemptAt = now.Add(backoff(cancellation.Attempts))
		data.Cancellations[cancellationKey] = cancellation
		if file, exists := data.Files[cancellation.FileID]; exists &&
			file.State == state.FileCancelPending &&
			file.UploadID == cancellation.UploadID {
			file.Attempts = cancellation.Attempts
			file.LastError = cancellation.LastError
			file.NextAttemptAt = cancellation.NextAttemptAt
			data.Files[cancellation.FileID] = file
		}
		data.Counters.CancelErrors++
		return nil
	})
	u.mu.Lock()
	u.metrics.LastError = cancelError.Error()
	u.mu.Unlock()
}

func (u *Uploader) finishInventoryDeclaration(manifestSHA256 string) error {
	now := u.now().UTC()
	if err := u.journal.Update(func(data *state.Journal) error {
		inventory, ok := data.Inventories[manifestSHA256]
		if !ok {
			return errors.New("route inventory disappeared from journal")
		}
		if !inventory.DeclaredAt.IsZero() {
			return nil
		}
		inventory.DeclaredAt = now
		inventory.Attempts = 0
		inventory.NextAttemptAt = time.Time{}
		inventory.LastError = ""
		data.Inventories[manifestSHA256] = inventory
		data.Counters.InventoriesDeclared++
		return nil
	}); err != nil {
		return err
	}
	u.mu.Lock()
	u.metrics.LastSuccessAt = now
	u.metrics.LastError = ""
	u.mu.Unlock()
	return nil
}

func (u *Uploader) recordInventoryFailure(manifestSHA256 string, inventoryError error) {
	now := u.now().UTC()
	_ = u.journal.Update(func(data *state.Journal) error {
		inventory, ok := data.Inventories[manifestSHA256]
		if !ok || !inventory.DeclaredAt.IsZero() {
			return nil
		}
		inventory.Attempts++
		inventory.LastError = inventoryError.Error()
		inventory.NextAttemptAt = now.Add(backoff(inventory.Attempts))
		data.Inventories[manifestSHA256] = inventory
		data.Counters.InventoryErrors++
		return nil
	})
	u.mu.Lock()
	u.metrics.LastError = inventoryError.Error()
	u.mu.Unlock()
}

func backoff(attempt int) time.Duration {
	if attempt < 1 {
		attempt = 1
	}
	delay := 5 * time.Second
	for i := 1; i < attempt && delay < 15*time.Minute; i++ {
		delay *= 2
	}
	if delay > 15*time.Minute {
		return 15 * time.Minute
	}
	return delay
}

func (u *Uploader) setActive(file string, offset int64) {
	u.mu.Lock()
	defer u.mu.Unlock()
	u.metrics.ActiveFile = file
	u.metrics.ActiveOffset = offset
}

func (u *Uploader) addBytes(count int64) {
	u.mu.Lock()
	defer u.mu.Unlock()
	u.metrics.SessionBytes += count
}

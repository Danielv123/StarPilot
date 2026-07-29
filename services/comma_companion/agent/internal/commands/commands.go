package commands

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"sort"
	"strings"
	"time"

	"starpilot.local/comma-companion-agent/internal/api"
	"starpilot.local/comma-companion-agent/internal/config"
	"starpilot.local/comma-companion-agent/internal/journal"
	"starpilot.local/comma-companion-agent/internal/policy"
	"starpilot.local/comma-companion-agent/internal/state"
)

type Action string

const (
	ActionNone             Action = ""
	ActionRestart          Action = "restart_agent"
	ActionRestartStarPilot Action = "restart_starpilot"
	ActionReboot           Action = "reboot_device"
	ActionShutdown         Action = "shutdown_device"
)

type ActionRequest struct {
	CommandID string
	Action    Action
	Reason    string
}

type Logger interface {
	Printf(format string, values ...any)
}

type Handler struct {
	config  config.Commands
	client  *api.Client
	journal *journal.Store
	policy  *policy.Reader
	rescan  chan<- struct{}
	logger  Logger
	now     func() time.Time
}

func New(
	cfg config.Commands,
	client *api.Client,
	store *journal.Store,
	policyReader *policy.Reader,
	rescan chan<- struct{},
	logger Logger,
) *Handler {
	return &Handler{
		config:  cfg,
		client:  client,
		journal: store,
		policy:  policyReader,
		rescan:  rescan,
		logger:  logger,
		now:     time.Now,
	}
}

func (h *Handler) Handle(ctx context.Context, command api.Command) ActionRequest {
	started := h.now().UTC()
	if err := validateCommandID(command.ID); err != nil {
		h.logger.Printf("command: rejected invalid command ID: %v", err)
		return ActionRequest{}
	}
	fingerprint, fingerprintErr := commandFingerprint(command)
	if existing, ok := h.journal.Snapshot().Commands[command.ID]; ok {
		if existing.Fingerprint != "" && (fingerprintErr != nil || existing.Fingerprint != fingerprint) {
			h.logger.Printf("command: ignored reused ID %s with different immutable fields", command.ID)
			return ActionRequest{}
		}
		if !existing.Reported {
			if err := h.report(ctx, existing); err != nil {
				return ActionRequest{}
			}
		}
		return h.claimAction(existing.ID)
	}
	record := state.CommandRecord{
		ID:          command.ID,
		Type:        command.Type,
		Fingerprint: fingerprint,
		State:       "succeeded",
		StartedAt:   started,
	}
	action := ActionNone
	if fingerprintErr != nil {
		record.State = "rejected"
		record.Error = fingerprintErr.Error()
	} else if err := validateCommand(command, started); err != nil {
		record.State = "rejected"
		record.Error = err.Error()
	} else {
		record.Message, record.Error, action = h.execute(command)
	}
	if record.Error != "" {
		record.State = "rejected"
		action = ActionNone
	}
	if action != ActionNone {
		record.State = "running"
		record.Action = string(action)
		record.ActionReason = actionReason(command, action)
		record.FinishedAt = time.Time{}
	} else {
		record.FinishedAt = h.now().UTC()
	}
	if err := h.journal.Update(func(data *state.Journal) error {
		data.Commands[record.ID] = record
		pruneCommands(data, 1024)
		return nil
	}); err != nil {
		h.logger.Printf("command: persist result %s: %v", command.ID, err)
		return ActionRequest{}
	}
	if err := h.report(ctx, record); err != nil {
		return ActionRequest{}
	}
	return h.claimAction(record.ID)
}

func (h *Handler) FlushPending(ctx context.Context) ActionRequest {
	snapshot := h.journal.Snapshot()
	records := make([]state.CommandRecord, 0, len(snapshot.Commands))
	for _, record := range snapshot.Commands {
		if !record.Reported {
			records = append(records, record)
		}
	}
	sort.Slice(records, func(i, j int) bool {
		return records[i].StartedAt.Before(records[j].StartedAt)
	})
	if len(records) > 0 {
		if err := h.report(ctx, records[0]); err != nil {
			return ActionRequest{}
		}
		return h.claimAction(records[0].ID)
	}
	for _, record := range snapshot.Commands {
		if record.State == "running" && record.Reported && record.Action != "" &&
			record.ActionQueuedAt.IsZero() {
			return h.claimAction(record.ID)
		}
	}
	return ActionRequest{}
}

func (h *Handler) execute(command api.Command) (string, string, Action) {
	switch command.Type {
	case "status":
		snapshot := h.journal.Snapshot()
		counts := make(map[state.FileState]int)
		var pendingBytes int64
		for _, file := range snapshot.Files {
			counts[file.State]++
			if file.State != state.FileDurable && file.State != state.FileCanceled && file.State != state.FileReleased {
				pendingBytes += file.Size - file.UploadOffset
			}
		}
		payload, _ := json.Marshal(map[string]any{
			"paused":        snapshot.Paused,
			"files":         counts,
			"pending_bytes": pendingBytes,
			"last_scan_at":  snapshot.LastScanAt,
		})
		return string(payload), "", ActionNone
	case "rescan":
		select {
		case h.rescan <- struct{}{}:
		default:
		}
		return "scan requested", "", ActionNone
	case "pause":
		if err := h.setPaused(true); err != nil {
			return "", err.Error(), ActionNone
		}
		return "uploads paused", "", ActionNone
	case "resume":
		if err := h.setPaused(false); err != nil {
			return "", err.Error(), ActionNone
		}
		return "uploads resumed", "", ActionNone
	case "retry_upload":
		changed, err := h.retryUploads(command.Args)
		if err != nil {
			return "", err.Error(), ActionNone
		}
		return fmt.Sprintf("%d upload(s) queued for retry", changed), "", ActionNone
	case "cancel_upload":
		changed, err := h.cancelUploads(command.Args)
		if err != nil {
			return "", err.Error(), ActionNone
		}
		return fmt.Sprintf("%d upload cancellation(s) queued; spool data was retained", changed), "", ActionNone
	case string(ActionRestart):
		if !h.config.AllowAgentRestart {
			return "", "agent restart is disabled by local config", ActionNone
		}
		return "agent restart is executing", "", ActionRestart
	case string(ActionRestartStarPilot):
		if !h.config.AllowStarPilotRestart {
			return "", "StarPilot restart is disabled by local config", ActionNone
		}
		if allowed, reason := h.policy.PowerActionAllowed(); !allowed {
			return "", "StarPilot restart offroad gate: " + reason, ActionNone
		}
		return "StarPilot restart is executing", "", ActionRestartStarPilot
	case string(ActionReboot), string(ActionShutdown):
		if !h.config.AllowPowerCommands {
			return "", "power commands are disabled by local config", ActionNone
		}
		if allowed, reason := h.policy.PowerActionAllowed(); !allowed {
			return "", "power command offroad gate: " + reason, ActionNone
		}
		if command.Type == string(ActionReboot) {
			return "device reboot is executing", "", ActionReboot
		}
		return "device shutdown is executing", "", ActionShutdown
	default:
		return "", "unsupported command type", ActionNone
	}
}

func validateCommandID(id string) error {
	if len(id) != 32 {
		return errors.New("command ID must be 32 lowercase hexadecimal characters")
	}
	decoded, err := hex.DecodeString(id)
	if err != nil || len(decoded) != 16 || strings.ToLower(id) != id {
		return errors.New("command ID must be 32 lowercase hexadecimal characters")
	}
	return nil
}

func commandFingerprint(command api.Command) (string, error) {
	canonical, err := json.Marshal(struct {
		ID              string         `json:"id"`
		Type            string         `json:"type"`
		IssuedAt        time.Time      `json:"issued_at"`
		ExpiresAt       time.Time      `json:"expires_at"`
		RequiresOffroad bool           `json:"requires_offroad"`
		Args            map[string]any `json:"args"`
	}{
		ID:              command.ID,
		Type:            command.Type,
		IssuedAt:        command.IssuedAt.UTC(),
		ExpiresAt:       command.ExpiresAt.UTC(),
		RequiresOffroad: command.RequiresOffroad,
		Args:            command.Args,
	})
	if err != nil {
		return "", fmt.Errorf("canonicalize command: %w", err)
	}
	digest := sha256.Sum256(canonical)
	return hex.EncodeToString(digest[:]), nil
}

func validateCommand(command api.Command, now time.Time) error {
	expectedOffroad, supported := requiresOffroad(command.Type)
	if !supported {
		return errors.New("unsupported command type")
	}
	if command.RequiresOffroad != expectedOffroad {
		return fmt.Errorf(
			"requires_offroad mismatch for %s: expected %t",
			command.Type,
			expectedOffroad,
		)
	}
	if command.State != "" && command.State != "delivered" {
		return fmt.Errorf("command state must be delivered, got %q", command.State)
	}
	if command.IssuedAt.IsZero() {
		return errors.New("command issued_at is required")
	}
	if command.ExpiresAt.IsZero() {
		return errors.New("command expires_at is required")
	}
	if command.ExpiresAt.Sub(command.IssuedAt) < 10*time.Second ||
		command.ExpiresAt.Sub(command.IssuedAt) > 24*time.Hour {
		return errors.New("command lifetime must be between 10 seconds and 24 hours")
	}
	if command.IssuedAt.After(now.Add(2 * time.Minute)) {
		return errors.New("command issued_at is too far in the future")
	}
	if !now.Before(command.ExpiresAt) {
		return errors.New("command expired")
	}
	return validateArgs(command.Type, command.Args)
}

func requiresOffroad(commandType string) (bool, bool) {
	switch commandType {
	case "status", "rescan", "pause", "resume", "retry_upload", "cancel_upload",
		string(ActionRestart):
		return false, true
	case string(ActionRestartStarPilot), string(ActionReboot), string(ActionShutdown):
		return true, true
	default:
		return false, false
	}
}

func validateArgs(commandType string, args map[string]any) error {
	switch commandType {
	case "status", "rescan", "pause", "resume", string(ActionRestart),
		string(ActionRestartStarPilot):
		if len(args) != 0 {
			return errors.New("command does not accept arguments")
		}
		return nil
	case string(ActionReboot), string(ActionShutdown):
		if len(args) != 1 {
			return errors.New("power command requires only a reason argument")
		}
		reason, ok := args["reason"].(string)
		if !ok || strings.TrimSpace(reason) == "" || len(reason) > 256 {
			return errors.New("power command reason must be a non-empty string up to 256 bytes")
		}
		return nil
	case "retry_upload", "cancel_upload":
		for key, value := range args {
			if key != "file_id" && key != "upload_id" && key != "scope" {
				return fmt.Errorf("upload command contains unsupported argument %q", key)
			}
			text, ok := value.(string)
			if !ok || strings.TrimSpace(text) == "" || len(text) > 256 {
				return fmt.Errorf("%s must be a non-empty string up to 256 bytes", key)
			}
		}
		fileID, _ := args["file_id"].(string)
		uploadID, _ := args["upload_id"].(string)
		scope, _ := args["scope"].(string)
		if fileID == "" && uploadID == "" && scope != "all" {
			return errors.New("file_id, upload_id, or scope=all is required")
		}
		if scope != "" && scope != "all" {
			return errors.New("scope must be all")
		}
		if scope == "all" && (fileID != "" || uploadID != "") {
			return errors.New("scope=all cannot be combined with file_id or upload_id")
		}
		return nil
	default:
		return errors.New("unsupported command type")
	}
}

func (h *Handler) claimAction(commandID string) ActionRequest {
	request := ActionRequest{}
	if err := h.journal.Update(func(data *state.Journal) error {
		record, ok := data.Commands[commandID]
		if !ok || record.State != "running" || !record.Reported || record.Action == "" ||
			!record.ActionQueuedAt.IsZero() {
			return nil
		}
		record.ActionQueuedAt = h.now().UTC()
		data.Commands[commandID] = record
		request = ActionRequest{
			CommandID: commandID,
			Action:    Action(record.Action),
			Reason:    record.ActionReason,
		}
		return nil
	}); err != nil {
		h.logger.Printf("command: claim action %s: %v", commandID, err)
		return ActionRequest{}
	}
	return request
}

func (h *Handler) MarkActionInvoked(commandID string) error {
	return h.journal.Update(func(data *state.Journal) error {
		record, ok := data.Commands[commandID]
		if !ok {
			return errors.New("command disappeared from journal")
		}
		if record.State != "running" || record.Action == "" || record.ActionQueuedAt.IsZero() {
			return errors.New("command action is not queued")
		}
		record.ActionInvokedAt = h.now().UTC()
		data.Commands[commandID] = record
		return nil
	})
}

func (h *Handler) CompleteAction(ctx context.Context, commandID string, actionError error) {
	var record state.CommandRecord
	if err := h.journal.Update(func(data *state.Journal) error {
		current, ok := data.Commands[commandID]
		if !ok {
			return errors.New("command disappeared from journal")
		}
		if current.State != "running" {
			return nil
		}
		current.FinishedAt = h.now().UTC()
		current.Reported = false
		if actionError != nil {
			current.State = "failed"
			current.Error = actionError.Error()
			current.Message = ""
		} else {
			current.State = "succeeded"
			current.Error = ""
			current.Message = actionSuccessMessage(Action(current.Action))
		}
		data.Commands[commandID] = current
		record = current
		return nil
	}); err != nil {
		h.logger.Printf("command: persist action result %s: %v", commandID, err)
		return
	}
	if record.ID != "" {
		_ = h.report(ctx, record)
	}
}

func (h *Handler) RecoverOnStartup() error {
	now := h.now().UTC()
	return h.journal.Update(func(data *state.Journal) error {
		for id, record := range data.Commands {
			if record.State != "running" || record.Action == "" {
				continue
			}
			switch {
			case Action(record.Action) == ActionRestart && !record.ActionInvokedAt.IsZero():
				record.State = "succeeded"
				record.Message = actionSuccessMessage(ActionRestart)
				record.Error = ""
			case privilegedAction(Action(record.Action)):
				// The independent helper binds this immutable command ID to
				// its action in a durable ledger. Requeueing after an agent
				// restart safely resolves a lost response without inventing
				// success or executing a different action.
				record.ActionQueuedAt = time.Time{}
				record.ActionInvokedAt = time.Time{}
				data.Commands[id] = record
				continue
			case !record.ActionInvokedAt.IsZero() || !record.ActionQueuedAt.IsZero():
				record.State = "failed"
				record.Message = ""
				record.Error = "agent restarted before the action result could be confirmed"
			default:
				continue
			}
			record.FinishedAt = now
			record.Reported = false
			data.Commands[id] = record
		}
		return nil
	})
}

func actionReason(command api.Command, action Action) string {
	if action == ActionReboot || action == ActionShutdown {
		if reason, ok := command.Args["reason"].(string); ok {
			return strings.TrimSpace(reason)
		}
	}
	if action == ActionRestartStarPilot {
		return "remote StarPilot restart"
	}
	return ""
}

func privilegedAction(action Action) bool {
	switch action {
	case ActionRestartStarPilot, ActionReboot, ActionShutdown:
		return true
	default:
		return false
	}
}

func actionSuccessMessage(action Action) string {
	switch action {
	case ActionRestart:
		return "agent restart completed"
	case ActionRestartStarPilot:
		return "StarPilot restart command completed"
	case ActionReboot:
		return "device reboot command completed"
	case ActionShutdown:
		return "device shutdown command completed"
	default:
		return "action completed"
	}
}

func pruneCommands(data *state.Journal, limit int) {
	if len(data.Commands) <= limit {
		return
	}
	records := make([]state.CommandRecord, 0, len(data.Commands))
	for _, record := range data.Commands {
		if record.Reported && commandTerminal(record.State) {
			records = append(records, record)
		}
	}
	sort.Slice(records, func(i, j int) bool {
		return records[i].FinishedAt.Before(records[j].FinishedAt)
	})
	for _, record := range records {
		if len(data.Commands) <= limit {
			break
		}
		delete(data.Commands, record.ID)
	}
}

func commandTerminal(commandState string) bool {
	switch commandState {
	case "succeeded", "failed", "rejected", "expired", "canceled":
		return true
	default:
		return false
	}
}

func (h *Handler) setPaused(paused bool) error {
	return h.journal.Update(func(data *state.Journal) error {
		data.Paused = paused
		return nil
	})
}

func uploadSelector(args map[string]any) (string, string, string, error) {
	fileID, _ := args["file_id"].(string)
	uploadID, _ := args["upload_id"].(string)
	scope, _ := args["scope"].(string)
	if fileID == "" && uploadID == "" && scope != "all" {
		return "", "", "", errors.New("file_id, upload_id, or scope=all is required")
	}
	if scope == "all" && (fileID != "" || uploadID != "") {
		return "", "", "", errors.New("scope=all cannot be combined with file_id or upload_id")
	}
	return fileID, uploadID, scope, nil
}

func uploadMatches(file state.File, fileID, uploadID, scope string) bool {
	if scope == "all" {
		return true
	}
	return (fileID == "" || file.ID == fileID) &&
		(uploadID == "" || file.UploadID == uploadID)
}

func (h *Handler) cancelUploads(args map[string]any) (int, error) {
	fileID, uploadID, scope, err := uploadSelector(args)
	if err != nil {
		return 0, err
	}
	now := h.now().UTC()
	changed := 0
	err = h.journal.Update(func(data *state.Journal) error {
		for id, file := range data.Files {
			if !uploadMatches(file, fileID, uploadID, scope) || file.State == state.FileDurable {
				continue
			}
			file.Attempts = 0
			file.NextAttemptAt = time.Time{}
			file.LastError = ""
			file.NeedsRespool = false
			file.CancelDeleteRecord = false
			if file.UploadID == "" {
				file.State = state.FileCanceled
				file.CancelRequestedAt = time.Time{}
				file.CancelNextState = ""
			} else {
				file.State = state.FileCancelPending
				file.CancelRequestedAt = now
				file.CancelNextState = state.FileCanceled
				queueCancellation(data, file.ID, file.UploadID, now)
			}
			data.Files[id] = file
			changed++
		}
		return nil
	})
	if err != nil {
		return 0, err
	}
	if changed == 0 {
		return 0, errors.New("no matching non-durable upload")
	}
	return changed, nil
}

func (h *Handler) retryUploads(args map[string]any) (int, error) {
	fileID, uploadID, scope, err := uploadSelector(args)
	if err != nil {
		return 0, err
	}
	now := h.now().UTC()
	changed := 0
	err = h.journal.Update(func(data *state.Journal) error {
		for id, file := range data.Files {
			if !uploadMatches(file, fileID, uploadID, scope) || file.State == state.FileDurable {
				continue
			}
			spoolReady := false
			if info, statErr := os.Lstat(file.SpoolPath); statErr == nil {
				spoolReady = info.Mode().IsRegular() && info.Size() == file.Size
			}
			sourceReady := false
			if info, statErr := os.Lstat(file.SourcePath); statErr == nil {
				sourceReady = info.Mode().IsRegular() && info.Size() == file.Size &&
					(file.ModTimeNS == 0 || info.ModTime().UnixNano() == file.ModTimeNS)
			}
			if !spoolReady && !sourceReady {
				continue
			}

			file.UploadAttempt++
			file.AutoRedeclarations = 0
			file.Attempts = 0
			file.NextAttemptAt = time.Time{}
			file.LastError = ""
			file.CancelDeleteRecord = false
			file.NeedsRespool = !spoolReady
			nextState := state.FileRetry
			if !spoolReady {
				nextState = state.FileReleased
			}
			if file.UploadID != "" {
				file.State = state.FileCancelPending
				file.CancelRequestedAt = now
				file.CancelNextState = nextState
				queueCancellation(data, file.ID, file.UploadID, now)
			} else {
				file.UploadID = ""
				file.UploadOffset = 0
				file.State = nextState
				file.CancelRequestedAt = time.Time{}
				file.CancelNextState = ""
			}
			data.Files[id] = file
			changed++
		}
		return nil
	})
	if err != nil {
		return 0, err
	}
	if changed == 0 {
		return 0, errors.New("no matching non-durable upload with retained spool or source data")
	}
	return changed, nil
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

func (h *Handler) report(ctx context.Context, record state.CommandRecord) error {
	result := api.CommandResult{
		State:   record.State,
		Message: record.Message,
		Error:   record.Error,
	}
	if !record.StartedAt.IsZero() {
		started := record.StartedAt
		result.StartedAt = &started
	}
	if !record.FinishedAt.IsZero() {
		finished := record.FinishedAt
		result.FinishedAt = &finished
	}
	if err := h.client.ReportCommand(ctx, record.ID, result); err != nil {
		h.logger.Printf("command: report result %s: %v", record.ID, err)
		return err
	}
	if err := h.journal.Update(func(data *state.Journal) error {
		current, ok := data.Commands[record.ID]
		if !ok {
			return errors.New("command disappeared from journal")
		}
		if current.State != record.State {
			return nil
		}
		current.Reported = true
		data.Commands[record.ID] = current
		return nil
	}); err != nil {
		h.logger.Printf("command: mark result reported %s: %v", record.ID, err)
		return err
	}
	return nil
}

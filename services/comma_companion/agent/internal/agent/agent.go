package agent

import (
	"context"
	"errors"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"runtime"
	"time"

	"starpilot.local/comma-companion-agent/internal/api"
	"starpilot.local/comma-companion-agent/internal/commands"
	"starpilot.local/comma-companion-agent/internal/config"
	"starpilot.local/comma-companion-agent/internal/control"
	"starpilot.local/comma-companion-agent/internal/hoststats"
	"starpilot.local/comma-companion-agent/internal/instance"
	"starpilot.local/comma-companion-agent/internal/journal"
	"starpilot.local/comma-companion-agent/internal/policy"
	"starpilot.local/comma-companion-agent/internal/scanner"
	"starpilot.local/comma-companion-agent/internal/spoolreconcile"
	"starpilot.local/comma-companion-agent/internal/state"
	"starpilot.local/comma-companion-agent/internal/storageguard"
	"starpilot.local/comma-companion-agent/internal/uploader"
)

var ErrRestartRequested = errors.New("agent restart requested")

type Agent struct {
	config    config.Config
	version   string
	logger    *log.Logger
	journal   *journal.Store
	policy    *policy.Reader
	scanner   *scanner.Scanner
	uploader  *uploader.Uploader
	commands  *commands.Handler
	control   *control.Client
	client    *api.Client
	storage   *storageguard.Guard
	host      *hoststats.Collector
	lock      *instance.Lock
	rescan    chan struct{}
	actions   chan commands.ActionRequest
	reconcile spoolreconcile.Result
	ready     bool
}

func New(cfg config.Config, version string, logger *log.Logger) (*Agent, error) {
	if err := cfg.ValidateDevicePaths(); err != nil {
		return nil, err
	}
	if err := os.MkdirAll(cfg.SpoolDir, 0o700); err != nil {
		return nil, fmt.Errorf("create spool directory: %w", err)
	}
	processLock, err := instance.Acquire(filepath.Join(cfg.SpoolDir, "agent.lock"))
	if err != nil {
		return nil, err
	}
	keepLock := false
	defer func() {
		if !keepLock {
			_ = processLock.Close()
		}
	}()
	if cfg.ReadyFile != "" {
		if err := os.Remove(cfg.ReadyFile); err != nil && !errors.Is(err, os.ErrNotExist) {
			return nil, fmt.Errorf("clear readiness file: %w", err)
		}
	}
	store, err := journal.Open(cfg.JournalPath)
	if err != nil {
		return nil, err
	}
	if cfg.UserAgent == "" || cfg.UserAgent == "comma-companion-agent/dev" {
		cfg.UserAgent = "comma-companion-agent/" + version
	}
	for _, root := range cfg.Roots {
		if _, err := os.Stat(root.Path); errors.Is(err, os.ErrNotExist) {
			continue
		} else if err != nil {
			return nil, fmt.Errorf("stat logging root %s: %w", root.Name, err)
		}
		same, err := storageguard.SameFilesystem(root.Path, cfg.SpoolDir)
		if err != nil {
			return nil, fmt.Errorf("compare logging root %s and spool filesystem: %w", root.Name, err)
		}
		if !same {
			return nil, fmt.Errorf("logging root %s and spool_dir must share a filesystem for hardlinks", root.Name)
		}
	}
	reconcileResult, err := spoolreconcile.Reconcile(
		cfg.SpoolDir,
		store,
		logger,
		spoolreconcile.DefaultMaxEntries,
	)
	if err != nil {
		return nil, fmt.Errorf("reconcile spool: %w", err)
	}
	if reconcileResult.OrphansQuarantined > 0 ||
		reconcileResult.TerminalLinksRemoved > 0 ||
		reconcileResult.CleanupErrors > 0 {
		logger.Printf(
			"spool reconcile: scanned=%d adopted=%d removed=%d quarantined=%d errors=%d truncated=%t",
			reconcileResult.Scanned,
			reconcileResult.Adopted,
			reconcileResult.TerminalLinksRemoved,
			reconcileResult.OrphansQuarantined,
			reconcileResult.CleanupErrors,
			reconcileResult.Truncated,
		)
	}
	if compacted, compactErr := store.CompactTerminalRecords(256); compactErr != nil {
		return nil, fmt.Errorf("compact terminal journal records: %w", compactErr)
	} else if compacted > 0 {
		logger.Printf("journal: compacted %d terminal record(s) whose source and spool links are gone", compacted)
	}
	policyReader := policy.New(cfg.Policy)
	client := api.New(cfg.ServerURL, cfg.DeviceID, cfg.Token, cfg.UserAgent, cfg.HTTPTimeout.Duration)
	rescan := make(chan struct{}, 1)
	commandHandler := commands.New(cfg.Commands, client, store, policyReader, rescan, logger)
	if err := commandHandler.RecoverOnStartup(); err != nil {
		return nil, fmt.Errorf("recover command actions: %w", err)
	}
	var controlClient *control.Client
	if cfg.Commands.AllowStarPilotRestart || cfg.Commands.AllowPowerCommands {
		controlClient = control.NewClient(
			cfg.Commands.ControlSocket,
			cfg.ControlToken,
			45*time.Second,
		)
	}
	result := &Agent{
		config:    cfg,
		version:   version,
		logger:    logger,
		journal:   store,
		policy:    policyReader,
		scanner:   scanner.New(cfg, store, logger),
		uploader:  uploader.New(client, store, policyReader, cfg.ChunkSize, logger),
		commands:  commandHandler,
		control:   controlClient,
		client:    client,
		storage:   storageguard.New(cfg.Storage, cfg.SpoolDir, store, logger),
		host:      hoststats.New(),
		lock:      processLock,
		rescan:    rescan,
		actions:   make(chan commands.ActionRequest, 1),
		reconcile: reconcileResult,
	}
	keepLock = true
	return result, nil
}

func (a *Agent) Run(ctx context.Context) error {
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	defer a.lock.Close()
	a.storage.Enforce()
	go a.scanLoop(ctx)
	go a.uploadLoop(ctx)
	go a.heartbeatLoop(ctx)

	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case request := <-a.actions:
			switch request.Action {
			case commands.ActionRestart:
				if err := a.commands.MarkActionInvoked(request.CommandID); err != nil {
					a.commands.CompleteAction(ctx, request.CommandID, err)
					continue
				}
				return ErrRestartRequested
			case commands.ActionRestartStarPilot, commands.ActionReboot, commands.ActionShutdown:
				if a.control == nil {
					a.commands.CompleteAction(
						ctx,
						request.CommandID,
						errors.New("privileged control helper is not configured"),
					)
					continue
				}
				if err := a.commands.MarkActionInvoked(request.CommandID); err != nil {
					a.commands.CompleteAction(ctx, request.CommandID, err)
					continue
				}
				err := a.control.Execute(
					ctx,
					request.CommandID,
					control.Operation(request.Action),
					request.Reason,
				)
				a.commands.CompleteAction(ctx, request.CommandID, err)
			default:
				a.commands.CompleteAction(
					ctx,
					request.CommandID,
					fmt.Errorf("unsupported local action %q", request.Action),
				)
			}
		}
	}
}

func (a *Agent) scanLoop(ctx context.Context) {
	timer := time.NewTimer(0)
	defer timer.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-a.rescan:
		case <-timer.C:
		}
		status := a.policy.Read()
		storageStatus := a.storage.Enforce()
		if a.reconcile.Truncated {
			a.logger.Printf("scan: startup spool reconciliation was truncated; manual review and restart required")
			timer.Reset(a.config.ScanInterval.Duration)
			continue
		}
		if a.config.Policy.UploadOnlyOffroad &&
			(!status.OffroadKnown || !status.Offroad || !status.OffroadSourceFresh) {
			timer.Reset(a.config.ScanInterval.Duration)
			continue
		}
		if !storageStatus.AllowNew {
			a.logger.Printf("scan: storage safety blocked new spool links: %s", storageStatus.Reason)
			timer.Reset(a.config.ScanInterval.Duration)
			continue
		}
		result := a.scanner.Scan(ctx, status.OffroadKnown && status.Offroad && status.OffroadSourceFresh)
		a.storage.Enforce()
		if compacted, err := a.journal.CompactTerminalRecords(256); err != nil {
			a.logger.Printf("journal: compact terminal records: %v", err)
		} else if compacted > 0 {
			a.logger.Printf("journal: compacted %d terminal record(s)", compacted)
		}
		if result.Errors > 0 || result.FilesSpooled > 0 {
			a.logger.Printf(
				"scan: seen=%d spooled=%d bytes=%d errors=%d",
				result.FilesSeen,
				result.FilesSpooled,
				result.BytesSpooled,
				result.Errors,
			)
		}
		timer.Reset(a.config.ScanInterval.Duration)
	}
}

func (a *Agent) uploadLoop(ctx context.Context) {
	timer := time.NewTimer(0)
	defer timer.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-timer.C:
		}
		didWork, err := a.uploader.ProcessOnce(ctx)
		if err != nil {
			a.logger.Printf("upload: %v", err)
		}
		delay := a.config.UploadPollInterval.Duration
		if didWork && err == nil {
			delay = 0
		}
		timer.Reset(delay)
	}
}

func (a *Agent) heartbeatLoop(ctx context.Context) {
	timer := time.NewTimer(0)
	defer timer.Stop()
	failures := 0
	for {
		select {
		case <-ctx.Done():
			return
		case <-timer.C:
		}
		a.enqueueAction(ctx, a.commands.FlushPending(ctx))
		policyStatus := a.policy.Read()
		request := a.heartbeat(policyStatus)
		response, err := a.client.Heartbeat(ctx, a.config.LongPollTimeout.Duration, request)
		if err != nil {
			failures++
			a.logger.Printf("heartbeat: %v", err)
			timer.Reset(heartbeatBackoff(a.config.HeartbeatInterval.Duration, failures))
			continue
		}
		failures = 0
		a.markReady()
	commandBatch:
		for _, command := range response.Commands {
			request := a.commands.Handle(ctx, command)
			if request.Action != commands.ActionNone {
				a.enqueueAction(ctx, request)
				break commandBatch
			}
		}
		// Do not assume the server honored long polling. A local floor prevents
		// an immediate-response backend from creating a tight heartbeat loop.
		timer.Reset(a.config.HeartbeatInterval.Duration)
	}
}

func (a *Agent) markReady() {
	if a.ready || a.config.ReadyFile == "" {
		return
	}
	directory := filepath.Dir(a.config.ReadyFile)
	temporary, err := os.CreateTemp(directory, ".ready-*.tmp")
	if err != nil {
		a.logger.Printf("heartbeat: create readiness file: %v", err)
		return
	}
	temporaryPath := temporary.Name()
	defer os.Remove(temporaryPath)
	if err := temporary.Chmod(0o600); err != nil {
		_ = temporary.Close()
		a.logger.Printf("heartbeat: secure readiness file: %v", err)
		return
	}
	if _, err := temporary.WriteString(time.Now().UTC().Format(time.RFC3339Nano) + "\n"); err != nil {
		_ = temporary.Close()
		a.logger.Printf("heartbeat: write readiness file: %v", err)
		return
	}
	if err := temporary.Sync(); err != nil {
		_ = temporary.Close()
		a.logger.Printf("heartbeat: sync readiness file: %v", err)
		return
	}
	if err := temporary.Close(); err != nil {
		a.logger.Printf("heartbeat: close readiness file: %v", err)
		return
	}
	if err := os.Rename(temporaryPath, a.config.ReadyFile); err != nil {
		a.logger.Printf("heartbeat: publish readiness file: %v", err)
		return
	}
	if handle, err := os.Open(directory); err == nil {
		_ = handle.Sync()
		_ = handle.Close()
	}
	a.ready = true
}

func (a *Agent) enqueueAction(ctx context.Context, request commands.ActionRequest) {
	if request.Action == commands.ActionNone {
		return
	}
	select {
	case a.actions <- request:
	default:
		err := fmt.Errorf("local action queue is busy")
		a.logger.Printf("command: reject queued action %s: %v", request.Action, err)
		a.commands.CompleteAction(ctx, request.CommandID, err)
	}
}

func heartbeatBackoff(minimum time.Duration, failures int) time.Duration {
	if minimum <= 0 {
		minimum = 5 * time.Second
	}
	delay := minimum
	for i := 1; i < failures && delay < 5*time.Minute; i++ {
		delay *= 2
	}
	if delay > 5*time.Minute {
		return 5 * time.Minute
	}
	return delay
}

func (a *Agent) heartbeat(policyStatus policy.Status) api.HeartbeatRequest {
	snapshot := a.journal.Snapshot()
	uploadMetrics := a.uploader.SnapshotMetrics()
	storageStatus := a.storage.Snapshot()
	hostStatus := a.host.Collect()
	counts := make(map[state.FileState]int)
	var pendingBytes int64
	for _, file := range snapshot.Files {
		counts[file.State]++
		if file.State != state.FileDurable && file.State != state.FileCanceled && file.State != state.FileReleased {
			pendingBytes += file.Size - file.UploadOffset
		}
	}
	inventoryCounts := make(map[string]int)
	latestInventoryByRoute := make(map[string]state.RouteInventory)
	pendingInventories := 0
	var lastInventoryCapturedAt time.Time
	var lastInventoryDeclaredAt time.Time
	for _, inventory := range snapshot.Inventories {
		inventoryCounts[inventory.Manifest.State]++
		if inventory.DeclaredAt.IsZero() {
			pendingInventories++
		} else if inventory.DeclaredAt.After(lastInventoryDeclaredAt) {
			lastInventoryDeclaredAt = inventory.DeclaredAt
		}
		if inventory.CapturedAt.After(lastInventoryCapturedAt) {
			lastInventoryCapturedAt = inventory.CapturedAt
		}
		latest, exists := latestInventoryByRoute[inventory.Manifest.RouteName]
		if !exists || inventory.Manifest.Generation > latest.Manifest.Generation {
			latestInventoryByRoute[inventory.Manifest.RouteName] = inventory
		}
	}
	latestInventoryCounts := make(map[string]int)
	for _, inventory := range latestInventoryByRoute {
		latestInventoryCounts[inventory.Manifest.State]++
	}
	agentState := "idle"
	switch {
	case snapshot.Paused:
		agentState = "paused"
	case storageStatus.Pressure:
		agentState = "storage_pressure"
	case !policyStatus.UploadAllowed:
		agentState = "policy_blocked"
	case uploadMetrics.ActiveFile != "":
		agentState = "uploading"
	case pendingBytes > 0:
		agentState = "queued"
	}
	var memory runtime.MemStats
	runtime.ReadMemStats(&memory)
	capabilities := []string{
		"resumable_upload_v1",
		"sha256",
		"hardlink_spool",
		"typed_commands_v1",
		"storage_guard_v1",
		"route_inventory_v1",
		"command_status",
		"command_rescan",
		"command_pause",
		"command_resume",
		"command_retry_upload",
		"command_cancel_upload",
	}
	if a.config.Commands.AllowAgentRestart {
		capabilities = append(capabilities, "command_restart_agent")
	}
	var offroad *bool
	if policyStatus.OffroadKnown && policyStatus.OffroadSourceFresh {
		value := policyStatus.Offroad
		offroad = &value
	}
	temperature := maximumTemperature(hostStatus)
	battery := batteryPercent(hostStatus)
	return api.HeartbeatRequest{
		AgentVersion: a.version,
		Timestamp:    time.Now().UTC(),
		State:        agentState,
		Capabilities: capabilities,
		Metrics: map[string]any{
			"paused":                     snapshot.Paused,
			"agent_build_version":        a.version,
			"file_counts":                counts,
			"pending_bytes":              pendingBytes,
			"spool_bytes":                storageStatus.RetainedBytes,
			"spool_capacity_bytes":       a.config.Storage.MaxRetainedBytes,
			"bytes_uploaded_total":       snapshot.Counters.BytesUploaded,
			"files_durable_total":        snapshot.Counters.FilesDurable,
			"scan_errors_total":          snapshot.Counters.ScanErrors,
			"upload_errors_total":        snapshot.Counters.UploadErrors,
			"files_released_total":       snapshot.Counters.FilesReleased,
			"cancel_errors_total":        snapshot.Counters.CancelErrors,
			"spool_orphans_total":        snapshot.Counters.OrphansQuarantined,
			"spool_cleanup_errors_total": snapshot.Counters.SpoolCleanupErrors,
			"spool_reconciled_total":     snapshot.Counters.SpoolFilesReconciled,
			"files_compacted_total":      snapshot.Counters.FilesCompacted,
			"route_inventory_counts":     inventoryCounts,
			"route_inventory_latest":     latestInventoryCounts,
			"route_inventories_pending":  pendingInventories,
			"route_inventories_total":    len(snapshot.Inventories),
			"inventories_captured_total": snapshot.Counters.InventoriesCaptured,
			"inventories_declared_total": snapshot.Counters.InventoriesDeclared,
			"inventory_errors_total":     snapshot.Counters.InventoryErrors,
			"last_inventory_captured_at": lastInventoryCapturedAt,
			"last_inventory_declared_at": lastInventoryDeclaredAt,
			"spool_quarantine_bytes":     storageStatus.QuarantineBytes,
			"spool_quarantine_files":     storageStatus.QuarantineFiles,
			"spool_quarantine_truncated": storageStatus.QuarantineTruncated,
			"spool_reconcile_truncated":  a.reconcile.Truncated,
			"upload_bytes_per_second":    uploadMetrics.UploadBytesPerS,
			"upload_bps":                 uploadMetrics.UploadBytesPerS,
			"active_file":                uploadMetrics.ActiveFile,
			"active_offset":              uploadMetrics.ActiveOffset,
			"last_scan_at":               snapshot.LastScanAt,
			"last_upload_at":             uploadMetrics.LastSuccessAt,
			"last_upload_error":          uploadMetrics.LastError,
			"memory_alloc_bytes":         memory.Alloc,
			"offroad_known":              policyStatus.OffroadKnown,
			"offroad_stable":             policyStatus.OffroadStable,
			"offroad_source_fresh":       policyStatus.OffroadSourceFresh,
			"offroad_since":              policyStatus.OffroadSince,
			"metered":                    policyStatus.Metered,
			"network_metered":            policyStatus.Metered,
			"metered_known":              policyStatus.MeteredKnown,
			"upload_allowed":             policyStatus.UploadAllowed,
			"policy_blocked_reason":      policyStatus.BlockedReason,
			"storage_free_bytes":         storageStatus.FreeBytes,
			"free_space_bytes":           storageStatus.FreeBytes,
			"storage_retained_bytes":     storageStatus.RetainedBytes,
			"storage_pressure":           storageStatus.Pressure,
			"storage_blocked_reason":     storageStatus.Reason,
			"host":                       hostStatus,
			"git_branch":                 hostStatus.StarPilot.Branch,
			"git_commit":                 hostStatus.StarPilot.Commit,
			"temperature_c":              temperature,
			"battery_percent":            battery,
			"current_route":              hostStatus.CurrentRoute,
			"manager_running":            hostStatus.ManagerRunning,
			"loggerd_running":            hostStatus.LoggerdRunning,
		},
		Offroad:         offroad,
		NetworkType:     policyStatus.NetworkType,
		SoftwareVersion: hostStatus.StarPilot.Commit,
	}
}

func maximumTemperature(snapshot hoststats.Snapshot) any {
	if len(snapshot.Temperatures) == 0 {
		return nil
	}
	maximum := snapshot.Temperatures[0].Celsius
	for _, temperature := range snapshot.Temperatures[1:] {
		if temperature.Celsius > maximum {
			maximum = temperature.Celsius
		}
	}
	return maximum
}

func batteryPercent(snapshot hoststats.Snapshot) any {
	for _, supply := range snapshot.PowerSupplies {
		if capacity, exists := supply.Metrics["capacity"]; exists {
			return capacity
		}
	}
	return nil
}

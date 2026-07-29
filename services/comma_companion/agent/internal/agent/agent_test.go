package agent

import (
	"io"
	"log"
	"path/filepath"
	"testing"
	"time"

	"starpilot.local/comma-companion-agent/internal/api"
	"starpilot.local/comma-companion-agent/internal/config"
	"starpilot.local/comma-companion-agent/internal/hoststats"
	"starpilot.local/comma-companion-agent/internal/journal"
	"starpilot.local/comma-companion-agent/internal/policy"
	"starpilot.local/comma-companion-agent/internal/state"
	"starpilot.local/comma-companion-agent/internal/storageguard"
	"starpilot.local/comma-companion-agent/internal/uploader"
)

func TestHeartbeatAdvertisesInventoryAndSpoolCapacity(t *testing.T) {
	spool := t.TempDir()
	store, err := journal.Open(filepath.Join(spool, "journal.json"))
	if err != nil {
		t.Fatal(err)
	}
	now := time.Date(2026, 7, 29, 12, 0, 0, 0, time.UTC)
	if err := store.Update(func(data *state.Journal) error {
		data.Inventories["old"] = state.RouteInventory{
			Manifest: state.RouteManifest{
				RouteName:  "route",
				Generation: 1,
				State:      "partial",
			},
			CapturedAt: now.Add(-time.Minute),
			DeclaredAt: now,
		}
		data.Inventories["new"] = state.RouteInventory{
			Manifest: state.RouteManifest{
				RouteName:  "route",
				Generation: 2,
				State:      "complete",
			},
			CapturedAt: now,
		}
		data.Counters.InventoriesCaptured = 2
		data.Counters.InventoriesDeclared = 1
		data.Counters.InventoryErrors = 3
		return nil
	}); err != nil {
		t.Fatal(err)
	}
	logger := log.New(io.Discard, "", 0)
	cfg := config.Defaults()
	cfg.Storage.MaxRetainedBytes = 123456789
	policyReader := policy.New(cfg.Policy)
	client := api.New("https://example.invalid", "device", "token", "test", time.Second)
	subject := &Agent{
		config:   cfg,
		version:  "test",
		journal:  store,
		policy:   policyReader,
		uploader: uploader.New(client, store, policyReader, 1024, logger),
		storage:  storageguard.New(cfg.Storage, spool, store, logger),
		host:     hoststats.New(),
	}
	heartbeat := subject.heartbeat(policy.Status{})
	if !containsCapability(heartbeat.Capabilities, "route_inventory_v1") {
		t.Fatalf("route inventory capability missing: %#v", heartbeat.Capabilities)
	}
	if heartbeat.Metrics["spool_capacity_bytes"] != int64(123456789) ||
		heartbeat.Metrics["route_inventories_pending"] != 1 ||
		heartbeat.Metrics["inventories_captured_total"] != int64(2) ||
		heartbeat.Metrics["inventories_declared_total"] != int64(1) ||
		heartbeat.Metrics["inventory_errors_total"] != int64(3) {
		t.Fatalf("inventory heartbeat metrics missing: %#v", heartbeat.Metrics)
	}
	latest, ok := heartbeat.Metrics["route_inventory_latest"].(map[string]int)
	if !ok || latest["complete"] != 1 || latest["partial"] != 0 {
		t.Fatalf("latest route inventory counts are wrong: %#v", heartbeat.Metrics["route_inventory_latest"])
	}
	subject.config.Commands.AllowStarPilotRestart = true
	subject.config.Commands.AllowPowerCommands = true
	heartbeat = subject.heartbeat(policy.Status{})
	for _, unavailable := range []string{
		"command_restart_starpilot",
		"command_reboot_device",
		"command_shutdown_device",
	} {
		if containsCapability(heartbeat.Capabilities, unavailable) {
			t.Fatalf("staged privileged capability was advertised: %q", unavailable)
		}
	}
}

func containsCapability(values []string, target string) bool {
	for _, value := range values {
		if value == target {
			return true
		}
	}
	return false
}

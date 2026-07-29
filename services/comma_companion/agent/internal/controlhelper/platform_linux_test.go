//go:build linux

package controlhelper

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"starpilot.local/comma-companion-agent/internal/control"
)

func TestCgroupContainsExactUnitComponent(t *testing.T) {
	raw := "0::/system.slice/comma-companion-agent.service\n"
	if !cgroupContainsUnit(raw, "comma-companion-agent.service") {
		t.Fatal("exact systemd unit component was not accepted")
	}
	for _, invalid := range []string{
		"comma-companion-agent",
		"system.slice/comma-companion-agent.service",
		"comma-companion-agent.service.evil",
		"",
	} {
		if cgroupContainsUnit(raw, invalid) {
			t.Fatalf("non-exact cgroup unit was accepted: %q", invalid)
		}
	}
}

func TestProductionExecutorUsesManagerDeferredRebootParam(t *testing.T) {
	paramsBase := t.TempDir()
	active := filepath.Join(paramsBase, "persistent")
	if err := os.Mkdir(active, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(active, filepath.Join(paramsBase, "d")); err != nil {
		t.Fatal(err)
	}
	execute := ProductionExecutor(paramsBase)
	if err := execute(context.Background(), control.RebootDevice, "test"); err != nil {
		t.Fatal(err)
	}
	raw, err := os.ReadFile(filepath.Join(active, "DoReboot"))
	if err != nil {
		t.Fatal(err)
	}
	if string(raw) != "1" {
		t.Fatalf("unexpected DoReboot value: %q", raw)
	}
	if _, err := os.Stat(filepath.Join(active, "DoUserReboot")); !os.IsNotExist(err) {
		t.Fatalf("unsafe DoUserReboot was written: %v", err)
	}
}

func TestSecurePandaProofRequiresRootOwnershipAndRestrictiveMode(t *testing.T) {
	path := filepath.Join(t.TempDir(), "panda-ignition.json")
	now := time.Now().UTC()
	writePandaProof(t, path, validPandaProof(now, 1))
	if os.Geteuid() != 0 {
		if _, err := securePandaProof(path); err == nil ||
			!strings.Contains(err.Error(), "root-owned") {
			t.Fatalf("non-root-owned proof was not rejected: %v", err)
		}
		return
	}
	if _, err := securePandaProof(path); err != nil {
		t.Fatalf("valid root-owned proof rejected: %v", err)
	}
	if err := os.Chmod(path, 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := securePandaProof(path); err == nil ||
		!strings.Contains(err.Error(), "permissions") {
		t.Fatalf("overly broad proof mode was not rejected: %v", err)
	}
}

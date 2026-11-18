package agent

import (
	"context"
	"io"
	"log"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"

	"starpilot.local/comma-companion-agent/internal/config"
	"starpilot.local/comma-companion-agent/internal/policy"
	"starpilot.local/comma-companion-agent/internal/scanner"
)

// Exercise real startup reconciliation, command recovery, scans and heartbeat
// generation against historical metadata, without uploading or driving. Run
// in an isolated Linux container: source paths in the copied journal are real
// device paths and must not point at a live archive during this benchmark.
func TestAgentMemoryProfile(t *testing.T) {
	source := os.Getenv("COMMA_JOURNAL_BENCHMARK")
	if source == "" {
		t.Skip("set COMMA_JOURNAL_BENCHMARK to a copied journal")
	}
	if os.Getenv("COMMA_BENCHMARK_ISOLATED") != "1" {
		t.Skip("requires an isolated filesystem")
	}
	cfg := config.Defaults()
	cfg.SpoolDir = t.TempDir()
	cfg.JournalPath = filepath.Join(cfg.SpoolDir, "journal.json")
	cfg.ServerURL = "http://127.0.0.1:1"
	cfg.DeviceID = "benchmark"
	cfg.Token = "benchmark"
	in, err := os.Open(source)
	if err != nil {
		t.Fatal(err)
	}
	out, err := os.Create(cfg.JournalPath)
	if err != nil {
		t.Fatal(err)
	}
	_, err = io.Copy(out, in)
	in.Close()
	out.Close()
	if err != nil {
		t.Fatal(err)
	}
	runtime.GC()
	var before runtime.MemStats
	runtime.ReadMemStats(&before)
	started := time.Now()
	subject, err := New(cfg, "memory-benchmark", log.New(io.Discard, "", 0))
	if err != nil {
		t.Fatal(err)
	}
	defer subject.lock.Close()
	t.Logf("startup completed in %s; inventories=%d", time.Since(started), len(subject.journal.View().Inventories))
	for range 5 {
		subject.scanner.ScanWithOptions(context.Background(), true, scanner.ScanOptions{AllowSpooling: false})
		_ = subject.heartbeat(policy.Status{})
	}
	if err := subject.journal.Checkpoint(); err != nil {
		t.Fatal(err)
	}
	var after runtime.MemStats
	runtime.ReadMemStats(&after)
	t.Logf("PROFILE elapsed=%s total_alloc_mib=%.1f gc_cycles=%d", time.Since(started), float64(after.TotalAlloc-before.TotalAlloc)/(1<<20), after.NumGC-before.NumGC)
	if status, err := os.ReadFile("/proc/self/status"); err == nil {
		for _, line := range strings.Split(string(status), "\n") {
			if strings.HasPrefix(line, "VmHWM:") {
				t.Log(line)
			}
		}
	}
}

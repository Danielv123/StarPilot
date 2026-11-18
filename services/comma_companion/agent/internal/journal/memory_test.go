package journal

import (
	"io"
	"os"
	"path/filepath"
	"runtime"
	"sync"
	"testing"
	"time"

	"starpilot.local/comma-companion-agent/internal/state"
)

// Run with GOMEMLIMIT=160MiB and COMMA_JOURNAL_BENCHMARK pointing at a copied
// device journal. Only temporary copies are modified; no agent or network runs.
func TestJournalMemoryProfile(t *testing.T) {
	source := os.Getenv("COMMA_JOURNAL_BENCHMARK")
	if source == "" {
		t.Skip("set COMMA_JOURNAL_BENCHMARK to a representative journal")
	}
	path := filepath.Join(t.TempDir(), "journal.json")
	in, err := os.Open(source)
	if err != nil {
		t.Fatal(err)
	}
	out, err := os.Create(path)
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
	var peak uint64
	stop := make(chan struct{})
	var wg sync.WaitGroup
	wg.Add(1)
	go func() {
		defer wg.Done()
		ticker := time.NewTicker(2 * time.Millisecond)
		defer ticker.Stop()
		for {
			var m runtime.MemStats
			runtime.ReadMemStats(&m)
			if used := m.Sys - m.HeapReleased; used > peak {
				peak = used
			}
			select {
			case <-stop:
				return
			case <-ticker.C:
			}
		}
	}()
	defer func() { close(stop); wg.Wait() }()
	started := time.Now()
	store, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	t.Logf("opened files=%d inventories=%d in %s", len(store.View().Files), len(store.View().Inventories), time.Since(started))
	original := store.View().Counters.ScanErrors
	for range 5 {
		if err := store.Update(func(data *state.Journal) error {
			data.Counters.ScanErrors++
			return nil
		}); err != nil {
			t.Fatal(err)
		}
	}
	if err := store.Checkpoint(); err != nil {
		t.Fatal(err)
	}
	store = nil
	runtime.GC()
	reopened, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	if reopened.View().Counters.ScanErrors != original+5 {
		t.Fatal("updates lost on reopen")
	}
	var after runtime.MemStats
	runtime.ReadMemStats(&after)
	// Stop sampling before reading the peak, including under the race detector.
	stop <- struct{}{}
	wg.Wait()
	t.Logf("PROFILE elapsed=%s peak_go_mib=%.1f total_alloc_mib=%.1f gc_cycles=%d", time.Since(started), float64(peak)/(1<<20), float64(after.TotalAlloc-before.TotalAlloc)/(1<<20), after.NumGC-before.NumGC)
	runtime.KeepAlive(reopened)
}

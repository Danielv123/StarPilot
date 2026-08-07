package scanner

import (
	"context"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strconv"
	"testing"
	"time"

	"starpilot.local/comma-companion-agent/internal/config"
	"starpilot.local/comma-companion-agent/internal/journal"
	"starpilot.local/comma-companion-agent/internal/state"
)

type testLogger struct {
	t *testing.T
}

func (l testLogger) Printf(format string, values ...any) {
	l.t.Logf(format, values...)
}

func TestScanRequiresElapsedStabilityAndPrioritizesLogs(t *testing.T) {
	base := time.Date(2026, 7, 28, 12, 0, 0, 0, time.UTC)
	root := filepath.Join(t.TempDir(), "realdata")
	spool := filepath.Join(filepath.Dir(root), "spool")
	older := filepath.Join(root, "route--0")
	newer := filepath.Join(root, "route--1")
	mustMkdir(t, older)
	mustMkdir(t, newer)
	for _, name := range []string{"fcamera.hevc", "qcamera.ts", "rlog", "qlog"} {
		writeAt(t, filepath.Join(older, name), []byte(name), base.Add(-time.Hour))
	}
	writeAt(t, filepath.Join(newer, "qlog"), []byte("active"), base)

	cfg := testConfig(root, spool)
	cfg.MaxFilesPerScan = 1
	store, err := journal.Open(cfg.JournalPath)
	if err != nil {
		t.Fatal(err)
	}
	subject := New(cfg, store, testLogger{t})
	now := base
	subject.now = func() time.Time { return now }

	first := subject.Scan(context.Background(), false)
	if first.FilesSpooled != 0 {
		t.Fatalf("first observation spooled %d files", first.FilesSpooled)
	}
	now = base.Add(100 * time.Millisecond)
	second := subject.Scan(context.Background(), false)
	if second.FilesSpooled != 0 {
		t.Fatalf("rapid rescan bypassed elapsed stability: %#v", second)
	}
	now = base.Add(2 * time.Second)
	third := subject.Scan(context.Background(), false)
	if third.FilesSpooled != 1 {
		t.Fatalf("expected exactly one bounded spool, got %#v", third)
	}

	snapshot := store.Snapshot()
	var captured state.File
	for _, file := range snapshot.Files {
		captured = file
	}
	if captured.ArtifactType != "qlog" {
		t.Fatalf("risk priority captured %q first", captured.ArtifactType)
	}
	sourceInfo, err := os.Stat(captured.SourcePath)
	if err != nil {
		t.Fatal(err)
	}
	spoolInfo, err := os.Stat(captured.SpoolPath)
	if err != nil {
		t.Fatal(err)
	}
	if !os.SameFile(sourceInfo, spoolInfo) {
		t.Fatal("spooled file is not a hardlink to its source")
	}
	if !contains(captured.CompletionEvidence, "newer_segment") {
		t.Fatalf("missing completion evidence: %#v", captured.CompletionEvidence)
	}
}

func TestSegmentLockBlocksEveryArtifact(t *testing.T) {
	base := time.Date(2026, 7, 28, 12, 0, 0, 0, time.UTC)
	root := filepath.Join(t.TempDir(), "realdata")
	spool := filepath.Join(filepath.Dir(root), "spool")
	segment := filepath.Join(root, "route--0")
	mustMkdir(t, segment)
	writeAt(t, filepath.Join(segment, "rlog"), []byte("log"), base.Add(-time.Hour))
	writeAt(t, filepath.Join(segment, ".lock"), []byte{}, base.Add(-time.Hour))

	cfg := testConfig(root, spool)
	store, err := journal.Open(cfg.JournalPath)
	if err != nil {
		t.Fatal(err)
	}
	subject := New(cfg, store, testLogger{t})
	now := base
	subject.now = func() time.Time { return now }
	subject.Scan(context.Background(), true)
	now = base.Add(2 * time.Second)
	result := subject.Scan(context.Background(), true)
	if result.FilesSpooled != 0 || len(store.Snapshot().Files) != 0 {
		t.Fatalf("locked segment was spooled: %#v", result)
	}
}

func TestFairBatchReservesMediaSlot(t *testing.T) {
	eligible := make([]candidate, 0)
	for index := 0; index < 10; index++ {
		eligible = append(eligible, candidate{artifactType: "qlog", relativePath: string(rune('a' + index))})
	}
	eligible = append(eligible, candidate{artifactType: "video", camera: "qcamera", relativePath: "video"})
	selected := fairBatch(eligible, 8)
	if len(selected) != 8 || selected[7].artifactType != "video" {
		t.Fatalf("media slot was not reserved: %#v", selected)
	}
}

func TestScanReportsFullUnuploadedBacklogWithoutGrowingSpool(t *testing.T) {
	base := time.Date(2026, 8, 4, 12, 0, 0, 0, time.UTC)
	root := filepath.Join(t.TempDir(), "realdata")
	spool := filepath.Join(filepath.Dir(root), "spool")
	segment := filepath.Join(root, "route--0")
	mustMkdir(t, segment)
	writeAt(t, filepath.Join(segment, "qlog"), []byte("queued"), base.Add(-time.Hour))
	writeAt(t, filepath.Join(segment, "rlog"), []byte("backlog"), base.Add(-time.Hour))

	cfg := testConfig(root, spool)
	store, err := journal.Open(cfg.JournalPath)
	if err != nil {
		t.Fatal(err)
	}
	subject := New(cfg, store, testLogger{t})
	subject.now = func() time.Time { return base }

	result := subject.ScanWithOptions(
		context.Background(),
		true,
		ScanOptions{AllowSpooling: false},
	)
	if !result.ScanComplete || result.UnuploadedFiles != 2 || result.UnuploadedBytes != 13 {
		t.Fatalf("full backlog was not measured: %#v", result)
	}
	snapshot := store.Snapshot()
	if result.FilesSpooled != 0 || len(snapshot.Files) != 0 {
		t.Fatalf("measurement-only scan grew the protected spool: %#v", result)
	}
	if len(snapshot.Observations) != 0 || !snapshot.LastScanAt.IsZero() {
		t.Fatalf("measurement-only scan rewrote stability journal: %#v", snapshot)
	}
}

func TestStorageBlockedScanStillCapturesInventory(t *testing.T) {
	base := time.Date(2026, 8, 7, 12, 0, 0, 0, time.UTC)
	root := filepath.Join(t.TempDir(), "realdata")
	spool := filepath.Join(filepath.Dir(root), "spool")
	writeRouteFiles(t, root, "route", 0, base.Add(-time.Hour), "qlog", "rlog")
	cfg := testConfig(root, spool)
	cfg.MaxFilesPerScan = 100
	cfg.Inventory.ExpectedStreams = []config.InventoryStream{
		{RootName: "realdata", ArtifactType: "qlog"},
		{RootName: "realdata", ArtifactType: "rlog"},
	}
	store, err := journal.Open(cfg.JournalPath)
	if err != nil {
		t.Fatal(err)
	}
	subject := New(cfg, store, testLogger{t})
	now := base
	subject.now = func() time.Time { return now }
	subject.Scan(context.Background(), true)
	now = base.Add(2 * time.Second)
	subject.Scan(context.Background(), true)
	if err := store.Update(func(data *state.Journal) error {
		data.Inventories = make(map[string]state.RouteInventory)
		data.Observations = make(map[string]state.Observation)
		data.LastScanAt = time.Time{}
		return nil
	}); err != nil {
		t.Fatal(err)
	}

	subject = New(cfg, store, testLogger{t})
	now = base.Add(4 * time.Second)
	subject.now = func() time.Time { return now }
	first := subject.ScanWithOptions(context.Background(), true, ScanOptions{AllowSpooling: false})
	now = base.Add(6 * time.Second)
	second := subject.ScanWithOptions(context.Background(), true, ScanOptions{AllowSpooling: false})
	if first.FilesSpooled != 0 || second.FilesSpooled != 0 {
		t.Fatalf("storage-blocked scan changed the spool: first=%#v second=%#v", first, second)
	}
	if _, found := latestRouteInventory(store.Snapshot(), "route"); !found {
		t.Fatalf(
			"storage-blocked scan did not capture a stable route inventory: observations=%#v files=%#v",
			subject.observations,
			store.Snapshot().Files,
		)
	}
}

func TestPrunedDurableRouteInventoryIsRecoveredFromJournal(t *testing.T) {
	base := time.Date(2026, 8, 7, 12, 0, 0, 0, time.UTC)
	root := filepath.Join(t.TempDir(), "realdata")
	spool := filepath.Join(filepath.Dir(root), "spool")
	writeRouteFiles(t, root, "route", 0, base.Add(-time.Hour), "qlog", "rlog")
	cfg := testConfig(root, spool)
	cfg.MaxFilesPerScan = 100
	cfg.Inventory.ExpectedStreams = []config.InventoryStream{
		{RootName: "realdata", ArtifactType: "qlog"},
		{RootName: "realdata", ArtifactType: "rlog"},
	}
	store, err := journal.Open(cfg.JournalPath)
	if err != nil {
		t.Fatal(err)
	}
	subject := New(cfg, store, testLogger{t})
	now := base
	subject.now = func() time.Time { return now }
	subject.Scan(context.Background(), true)
	now = base.Add(2 * time.Second)
	subject.Scan(context.Background(), true)
	if err := store.Update(func(data *state.Journal) error {
		for id, file := range data.Files {
			file.State = state.FileDurable
			file.DurableAt = now
			data.Files[id] = file
		}
		data.Inventories = make(map[string]state.RouteInventory)
		return nil
	}); err != nil {
		t.Fatal(err)
	}
	if err := os.RemoveAll(filepath.Join(root, "route--0")); err != nil {
		t.Fatal(err)
	}

	subject = New(cfg, store, testLogger{t})
	now = base.Add(4 * time.Second)
	subject.now = func() time.Time { return now }
	result := subject.ScanWithOptions(context.Background(), true, ScanOptions{AllowSpooling: false})
	if result.Errors != 0 {
		t.Fatalf("journal-only recovery failed: %#v", result)
	}
	inventory, found := latestRouteInventory(store.Snapshot(), "route")
	if !found || inventory.Manifest.State != "complete" {
		t.Fatalf(
			"durable pruned route was not recovered: found=%v inventory=%#v files=%#v",
			found,
			inventory,
			store.Snapshot().Files,
		)
	}
}

func TestParsesObservedFrogPilotSegmentName(t *testing.T) {
	relative := filepath.Join("000000dc--fe7070223b--99", "rlog")
	route, segment, directory := parseSegment(relative)
	if route != "000000dc--fe7070223b" || segment == nil || *segment != 99 ||
		directory != "000000dc--fe7070223b--99" {
		t.Fatalf("unexpected parse: route=%q segment=%v directory=%q", route, segment, directory)
	}
}

func TestReleasedRetryRespoolsSourceWithoutLosingUploadGeneration(t *testing.T) {
	base := time.Date(2026, 7, 28, 12, 0, 0, 0, time.UTC)
	root := filepath.Join(t.TempDir(), "realdata")
	spool := filepath.Join(filepath.Dir(root), "spool")
	source := filepath.Join(root, "artifact.bin")
	mustMkdir(t, root)
	writeAt(t, source, []byte("retry"), base.Add(-time.Hour))

	cfg := testConfig(root, spool)
	store, err := journal.Open(cfg.JournalPath)
	if err != nil {
		t.Fatal(err)
	}
	subject := New(cfg, store, testLogger{t})
	now := base
	subject.now = func() time.Time { return now }
	subject.Scan(context.Background(), true)
	now = base.Add(2 * time.Second)
	if result := subject.Scan(context.Background(), true); result.FilesSpooled != 1 {
		t.Fatalf("initial spool failed: %#v", result)
	}
	snapshot := store.Snapshot()
	var file state.File
	for _, candidate := range snapshot.Files {
		file = candidate
	}
	if err := os.Remove(file.SpoolPath); err != nil {
		t.Fatal(err)
	}
	if err := store.Update(func(data *state.Journal) error {
		current := data.Files[file.ID]
		current.State = state.FileReleased
		current.NeedsRespool = true
		current.UploadAttempt = 3
		current.ReleasedAt = now
		data.Files[file.ID] = current
		return nil
	}); err != nil {
		t.Fatal(err)
	}
	now = now.Add(2 * time.Second)
	if result := subject.Scan(context.Background(), true); result.FilesSpooled != 0 {
		t.Fatalf("retry source bypassed storage-release backoff: %#v", result)
	}
	now = now.Add(cfg.ScanInterval.Duration)
	if result := subject.Scan(context.Background(), true); result.FilesSpooled != 1 {
		t.Fatalf("retry source was not respooled: %#v", result)
	}
	retried := store.Snapshot().Files[file.ID]
	if retried.State != state.FileSpooled || retried.NeedsRespool ||
		retried.UploadAttempt != 3 {
		t.Fatalf("respool lost retry generation: %#v", retried)
	}
	if _, err := os.Stat(retried.SpoolPath); err != nil {
		t.Fatalf("respooled link missing: %v", err)
	}
}

func TestRouteInventoryCompleteAndImmutableSupersedingGeneration(t *testing.T) {
	base := time.Date(2026, 7, 29, 12, 0, 0, 0, time.UTC)
	root := filepath.Join(t.TempDir(), "realdata")
	spool := filepath.Join(filepath.Dir(root), "spool")
	for segment := 0; segment < 2; segment++ {
		writeRouteFiles(
			t,
			root,
			"route",
			segment,
			base.Add(-time.Hour),
			"rlog.zst",
			"fcamera.hevc",
		)
	}
	cfg := testConfig(root, spool)
	cfg.MaxFilesPerScan = 100
	cfg.Inventory.ExpectedStreams = []config.InventoryStream{
		{RootName: "realdata", ArtifactType: "rlog"},
		{RootName: "realdata", ArtifactType: "video", Camera: "road"},
	}
	store, err := journal.Open(cfg.JournalPath)
	if err != nil {
		t.Fatal(err)
	}
	subject := New(cfg, store, testLogger{t})
	now := base
	subject.now = func() time.Time { return now }
	subject.Scan(context.Background(), true)
	now = base.Add(2 * time.Second)
	result := subject.Scan(context.Background(), true)
	if result.Errors != 0 || result.FilesSpooled != 4 {
		t.Fatalf("complete route was not captured: %#v", result)
	}
	snapshot := store.Snapshot()
	first, found := latestRouteInventory(snapshot, "route")
	if !found || len(snapshot.Inventories) != 1 {
		t.Fatalf("expected one route inventory, got %#v", snapshot.Inventories)
	}
	if first.Manifest.State != "complete" || first.Manifest.Generation != 1 ||
		len(first.Manifest.Segments) != 2 || len(first.Manifest.ExpectedStreams) != 2 {
		t.Fatalf("unexpected complete inventory: %#v", first.Manifest)
	}

	now = base.Add(3 * time.Second)
	subject.Scan(context.Background(), true)
	if len(store.Snapshot().Inventories) != 1 {
		t.Fatal("unchanged route created another immutable generation")
	}

	writeAt(
		t,
		filepath.Join(root, "route--0", "qcamera.ts"),
		[]byte("late-qcamera"),
		base.Add(-time.Hour),
	)
	now = base.Add(4 * time.Second)
	subject.Scan(context.Background(), true)
	if len(store.Snapshot().Inventories) != 1 {
		t.Fatal("unstable late file prematurely superseded the inventory")
	}
	now = base.Add(6 * time.Second)
	result = subject.Scan(context.Background(), true)
	if result.Errors != 0 || result.FilesSpooled != 1 {
		t.Fatalf("late file was not archived cleanly: %#v", result)
	}
	snapshot = store.Snapshot()
	second, found := latestRouteInventory(snapshot, "route")
	if !found || len(snapshot.Inventories) != 2 {
		t.Fatalf("late file did not create an immutable generation: %#v", snapshot.Inventories)
	}
	if second.Manifest.Generation != 2 || second.Manifest.State != "partial" ||
		second.Manifest.PreviousManifestSHA256 == nil ||
		*second.Manifest.PreviousManifestSHA256 != first.ManifestSHA256 {
		t.Fatalf("invalid superseding inventory chain: %#v", second.Manifest)
	}
	if !contains(second.Manifest.ClosureEvidence, "missing_expected_streams") {
		t.Fatalf("late intermittent stream was not reported missing: %#v", second.Manifest)
	}
	var qcameraMissing bool
	for _, segment := range second.Manifest.Segments {
		for _, stream := range segment.Streams {
			if segment.Number == 1 && stream.Role == "realdata|video|qcamera" &&
				stream.Status == "missing" {
				qcameraMissing = true
			}
		}
	}
	if !qcameraMissing {
		t.Fatal("superseding manifest lacks the explicit missing qcamera row")
	}
}

func TestRouteInventoryBackfillsAfterFinalGraceWithoutOffroadSignal(t *testing.T) {
	base := time.Date(2026, 7, 29, 12, 0, 0, 0, time.UTC)
	root := filepath.Join(t.TempDir(), "realdata")
	spool := filepath.Join(filepath.Dir(root), "spool")
	writeRouteFiles(t, root, "closed-route", 0, base.Add(-2*time.Hour), "rlog.zst", "qlog.zst")
	writeRouteFiles(t, root, "active-route", 0, base, "rlog.zst", "qlog.zst")

	cfg := testConfig(root, spool)
	cfg.MaxFilesPerScan = 100
	cfg.Inventory.ExpectedStreams = []config.InventoryStream{
		{RootName: "realdata", ArtifactType: "rlog"},
		{RootName: "realdata", ArtifactType: "qlog"},
	}
	store, err := journal.Open(cfg.JournalPath)
	if err != nil {
		t.Fatal(err)
	}
	subject := New(cfg, store, testLogger{t})
	now := base
	subject.now = func() time.Time { return now }

	subject.Scan(context.Background(), false)
	now = base.Add(2 * time.Second)
	result := subject.Scan(context.Background(), false)
	if result.Errors != 0 || result.FilesSpooled != 2 {
		t.Fatalf("closed route was not captured without offroad state: %#v", result)
	}
	snapshot := store.Snapshot()
	inventory, found := latestRouteInventory(snapshot, "closed-route")
	if !found {
		t.Fatal("closed route inventory was not backfilled without offroad state")
	}
	if contains(inventory.Manifest.ClosureEvidence, "offroad") ||
		!contains(inventory.Manifest.ClosureEvidence, "final_segment_grace") {
		t.Fatalf("unexpected closure evidence: %#v", inventory.Manifest.ClosureEvidence)
	}
	if _, found := latestRouteInventory(snapshot, "active-route"); found {
		t.Fatal("active route inventory was captured before final-segment grace elapsed")
	}
}

func TestRouteInventoryEmptyConfigIsExplicitlyPartial(t *testing.T) {
	manifest := captureTestRouteInventory(
		t,
		nil,
		map[int][]string{
			0: {"rlog.zst"},
			1: {"rlog.zst"},
		},
	)
	if manifest.State != "partial" ||
		manifest.CapabilitySource != "route_union_unconfigured" ||
		!contains(manifest.ClosureEvidence, "expected_streams_unconfigured") ||
		!contains(manifest.ClosureEvidence, "rlog_stream_unconfigured") {
		t.Fatalf("empty capability config was not fail-closed: %#v", manifest)
	}
}

func TestRouteInventoryFiltersProfilesToActiveAlternativeRoot(t *testing.T) {
	base := time.Date(2026, 7, 29, 12, 0, 0, 0, time.UTC)
	dir := t.TempDir()
	standardRoot := filepath.Join(dir, "realdata")
	hdRoot := filepath.Join(dir, "realdata_HD")
	konikRoot := filepath.Join(dir, "realdata_konik")
	spool := filepath.Join(dir, "spool")
	for segment := 0; segment < 2; segment++ {
		writeRouteFiles(t, standardRoot, "route", segment, base.Add(-time.Hour), "rlog.zst")
	}
	cfg := testConfig(standardRoot, spool)
	cfg.Roots = []config.Root{
		{Name: "realdata", Path: standardRoot},
		{Name: "realdata_HD", Path: hdRoot},
		{Name: "realdata_konik", Path: konikRoot},
	}
	cfg.Inventory.ExpectedStreams = []config.InventoryStream{
		{RootName: "realdata", ArtifactType: "rlog"},
		{RootName: "realdata_HD", ArtifactType: "rlog"},
		{RootName: "realdata_HD", ArtifactType: "video", Camera: "wide"},
		{RootName: "realdata_konik", ArtifactType: "rlog"},
	}
	cfg.MaxFilesPerScan = 100
	store, err := journal.Open(cfg.JournalPath)
	if err != nil {
		t.Fatal(err)
	}
	subject := New(cfg, store, testLogger{t})
	now := base
	subject.now = func() time.Time { return now }
	subject.Scan(context.Background(), true)
	now = base.Add(2 * time.Second)
	subject.Scan(context.Background(), true)
	inventory, found := latestRouteInventory(store.Snapshot(), "route")
	if !found {
		t.Fatal("route inventory was not captured")
	}
	if inventory.Manifest.State != "complete" ||
		!reflect.DeepEqual(inventory.Manifest.RootNames, []string{"realdata"}) {
		t.Fatalf("inactive alternative root polluted route readiness: %#v", inventory.Manifest)
	}
	if len(inventory.Manifest.ExpectedStreams) != 1 ||
		inventory.Manifest.ExpectedStreams[0].Role != "realdata|rlog|-" {
		t.Fatalf("inactive root roles were applied: %#v", inventory.Manifest.ExpectedStreams)
	}
}

func TestRouteInventoryRejectsMultipleActiveAlternativeRootsAsComplete(t *testing.T) {
	base := time.Date(2026, 7, 29, 12, 0, 0, 0, time.UTC)
	dir := t.TempDir()
	standardRoot := filepath.Join(dir, "realdata")
	hdRoot := filepath.Join(dir, "realdata_HD")
	spool := filepath.Join(dir, "spool")
	for segment := 0; segment < 2; segment++ {
		writeRouteFiles(t, standardRoot, "route", segment, base.Add(-time.Hour), "rlog.zst")
		writeRouteFiles(t, hdRoot, "route", segment, base.Add(-time.Hour), "rlog.zst")
	}
	cfg := testConfig(standardRoot, spool)
	cfg.Roots = []config.Root{
		{Name: "realdata", Path: standardRoot},
		{Name: "realdata_HD", Path: hdRoot},
	}
	cfg.Inventory.ExpectedStreams = []config.InventoryStream{
		{RootName: "realdata", ArtifactType: "rlog"},
		{RootName: "realdata_HD", ArtifactType: "rlog"},
	}
	cfg.MaxFilesPerScan = 100
	store, err := journal.Open(cfg.JournalPath)
	if err != nil {
		t.Fatal(err)
	}
	subject := New(cfg, store, testLogger{t})
	now := base
	subject.now = func() time.Time { return now }
	subject.Scan(context.Background(), true)
	now = base.Add(2 * time.Second)
	result := subject.Scan(context.Background(), true)
	if result.Errors != 0 || result.FilesSpooled != 4 {
		t.Fatalf("multi-root route was not captured cleanly: %#v", result)
	}
	captured, found := latestRouteInventory(store.Snapshot(), "route")
	if !found {
		t.Fatal("multi-root route inventory was not captured")
	}
	manifest := captured.Manifest
	if manifest.State != "partial" ||
		manifest.CapabilitySource != "configured+route_union" ||
		!reflect.DeepEqual(manifest.RootNames, []string{"realdata", "realdata_HD"}) ||
		!contains(manifest.ClosureEvidence, "multiple_active_log_roots") {
		t.Fatalf("multi-root conflict was not fail-closed: %#v", manifest)
	}
	if contains(manifest.ClosureEvidence, "missing_expected_streams") ||
		contains(manifest.ClosureEvidence, "expected_streams_unconfigured") {
		t.Fatalf("multi-root conflict was conflated with another closure failure: %#v", manifest)
	}
}

func TestRouteInventoryMakesGapsAndWholeRouteMissingStreamsExplicit(t *testing.T) {
	manifest := captureTestRouteInventory(
		t,
		[]config.InventoryStream{
			{RootName: "realdata", ArtifactType: "rlog"},
			{RootName: "realdata", ArtifactType: "video", Camera: "road"},
		},
		map[int][]string{
			0: {"rlog.zst"},
			2: {"rlog.zst"},
		},
	)
	if manifest.State != "partial" ||
		!reflect.DeepEqual(manifest.MissingSegmentNumbers, []int{1}) ||
		!contains(manifest.ClosureEvidence, "missing_segment_numbers") ||
		!contains(manifest.ClosureEvidence, "missing_expected_streams") {
		t.Fatalf("route gaps were not explicit: %#v", manifest)
	}
	for _, segment := range manifest.Segments {
		foundMissingRoad := false
		for _, stream := range segment.Streams {
			if stream.Role == "realdata|video|road" && stream.Status == "missing" &&
				stream.RelativePath == nil && stream.SHA256 == nil &&
				stream.Size == nil && stream.MTimeNS == nil {
				foundMissingRoad = true
			}
		}
		if !foundMissingRoad {
			t.Fatalf("segment %d lacks a null missing road row: %#v", segment.Number, segment.Streams)
		}
	}
}

func TestOptionalStreamBecomesRequiredOnlyAfterItIsObserved(t *testing.T) {
	streams := []config.InventoryStream{
		{RootName: "realdata", ArtifactType: "rlog"},
		{
			RootName:              "realdata",
			ArtifactType:          "video",
			Camera:                "driver",
			OptionalUntilObserved: true,
		},
	}
	withoutDriver := captureTestRouteInventory(
		t,
		streams,
		map[int][]string{0: {"rlog.zst"}, 1: {"rlog.zst"}},
	)
	if withoutDriver.State != "complete" ||
		contains(withoutDriver.ClosureEvidence, "missing_expected_streams") {
		t.Fatalf("never-observed optional stream made route partial: %#v", withoutDriver)
	}
	for _, expected := range withoutDriver.ExpectedStreams {
		if expected.Role == "realdata|video|driver" {
			t.Fatalf("never-observed optional stream was declared: %#v", withoutDriver.ExpectedStreams)
		}
	}

	intermittentDriver := captureTestRouteInventory(
		t,
		streams,
		map[int][]string{
			0: {"rlog.zst", "dcamera.hevc"},
			1: {"rlog.zst"},
		},
	)
	if intermittentDriver.State != "partial" ||
		!contains(intermittentDriver.ClosureEvidence, "missing_expected_streams") {
		t.Fatalf("observed optional stream did not become route-required: %#v", intermittentDriver)
	}
}

func TestPrunedRouteDropsStaleNeverObservedOptionalStream(t *testing.T) {
	base := time.Date(2026, 8, 7, 12, 0, 0, 0, time.UTC)
	root := filepath.Join(t.TempDir(), "realdata")
	spool := filepath.Join(filepath.Dir(root), "spool")
	writeRouteFiles(t, root, "route", 0, base.Add(-time.Hour), "rlog.zst")
	cfg := testConfig(root, spool)
	cfg.MaxFilesPerScan = 100
	cfg.Inventory.ExpectedStreams = []config.InventoryStream{
		{RootName: "realdata", ArtifactType: "rlog"},
		{RootName: "realdata", ArtifactType: "video", Camera: "driver"},
	}
	store, err := journal.Open(cfg.JournalPath)
	if err != nil {
		t.Fatal(err)
	}
	subject := New(cfg, store, testLogger{t})
	now := base
	subject.now = func() time.Time { return now }
	subject.Scan(context.Background(), true)
	now = base.Add(2 * time.Second)
	subject.Scan(context.Background(), true)
	first, found := latestRouteInventory(store.Snapshot(), "route")
	if !found || first.Manifest.State != "partial" || first.Manifest.Generation != 1 {
		t.Fatalf("required absent stream did not create the expected legacy inventory: %#v", first)
	}
	if err := os.RemoveAll(filepath.Join(root, "route--0")); err != nil {
		t.Fatal(err)
	}

	cfg.Inventory.ExpectedStreams[1].OptionalUntilObserved = true
	subject = New(cfg, store, testLogger{t})
	now = base.Add(4 * time.Second)
	subject.now = func() time.Time { return now }
	result := subject.ScanWithOptions(
		context.Background(),
		true,
		ScanOptions{AllowSpooling: false},
	)
	if result.Errors != 0 {
		t.Fatalf("optional-stream migration failed: %#v", result)
	}
	second, found := latestRouteInventory(store.Snapshot(), "route")
	if !found || second.Manifest.Generation != 2 || second.Manifest.State != "complete" {
		t.Fatalf("stale optional stream was not superseded: %#v", second)
	}
	if second.Manifest.PreviousManifestSHA256 == nil ||
		*second.Manifest.PreviousManifestSHA256 != first.ManifestSHA256 {
		t.Fatalf("optional-stream migration broke the generation chain: %#v", second.Manifest)
	}
	if contains(second.Manifest.ClosureEvidence, "missing_expected_streams") {
		t.Fatalf("stale missing-stream evidence survived migration: %#v", second.Manifest)
	}
	for _, expected := range second.Manifest.ExpectedStreams {
		if expected.Role == "realdata|video|driver" {
			t.Fatalf("never-observed optional role survived migration: %#v", second.Manifest)
		}
	}
}

func TestRouteInventoryWithoutConfiguredRLogStaysPartial(t *testing.T) {
	manifest := captureTestRouteInventory(
		t,
		[]config.InventoryStream{
			{RootName: "realdata", ArtifactType: "video", Camera: "road"},
		},
		map[int][]string{
			0: {"fcamera.hevc"},
			1: {"fcamera.hevc"},
		},
	)
	if manifest.State != "partial" ||
		!contains(manifest.ClosureEvidence, "rlog_stream_unconfigured") {
		t.Fatalf("video-only route was incorrectly complete: %#v", manifest)
	}
}

func TestSuggestExpectedStreamsMatchesObservedCommaCorpus(t *testing.T) {
	base := time.Date(2026, 7, 29, 12, 0, 0, 0, time.UTC)
	root := filepath.Join(t.TempDir(), "realdata")
	spool := filepath.Join(filepath.Dir(root), "spool")
	for segment := 0; segment < 4; segment++ {
		writeRouteFiles(
			t,
			root,
			"000000c6--d12fa4d148",
			segment,
			base.Add(-time.Hour),
			"rlog.zst",
			"qlog.zst",
			"fcamera.hevc",
			"ecamera.hevc",
			"qcamera.ts",
			"dcamera.hevc",
		)
	}
	cfg := testConfig(root, spool)
	suggestion, err := SuggestExpectedStreams(context.Background(), cfg, 4, base)
	if err != nil {
		t.Fatal(err)
	}
	roles := make([]string, 0, len(suggestion.ExpectedStreams))
	for _, stream := range suggestion.ExpectedStreams {
		camera := stream.Camera
		if camera == "" {
			camera = "-"
		}
		roles = append(roles, stream.RootName+"|"+stream.ArtifactType+"|"+camera)
	}
	expected := []string{
		"realdata|qlog|-",
		"realdata|rlog|-",
		"realdata|video|driver",
		"realdata|video|qcamera",
		"realdata|video|road",
		"realdata|video|wide",
	}
	if !reflect.DeepEqual(roles, expected) || suggestion.SampledSegments != 4 {
		t.Fatalf("unexpected suggested profile: roles=%#v suggestion=%#v", roles, suggestion)
	}
	for _, coverage := range suggestion.Coverage {
		if coverage.PresentSegments != 4 || coverage.SampledSegments != 4 {
			t.Fatalf("unexpected stream coverage: %#v", coverage)
		}
	}
}

func captureTestRouteInventory(
	t *testing.T,
	streams []config.InventoryStream,
	segments map[int][]string,
) state.RouteManifest {
	t.Helper()
	base := time.Date(2026, 7, 29, 12, 0, 0, 0, time.UTC)
	root := filepath.Join(t.TempDir(), "realdata")
	spool := filepath.Join(filepath.Dir(root), "spool")
	numbers := make([]int, 0, len(segments))
	for number := range segments {
		numbers = append(numbers, number)
	}
	sort.Ints(numbers)
	for _, number := range numbers {
		writeRouteFiles(t, root, "route", number, base.Add(-time.Hour), segments[number]...)
	}
	cfg := testConfig(root, spool)
	cfg.MaxFilesPerScan = 100
	cfg.Inventory.ExpectedStreams = append([]config.InventoryStream(nil), streams...)
	store, err := journal.Open(cfg.JournalPath)
	if err != nil {
		t.Fatal(err)
	}
	subject := New(cfg, store, testLogger{t})
	now := base
	subject.now = func() time.Time { return now }
	subject.Scan(context.Background(), true)
	now = base.Add(2 * time.Second)
	result := subject.Scan(context.Background(), true)
	if result.Errors != 0 {
		t.Fatalf("route capture errors: %#v", result)
	}
	inventory, found := latestRouteInventory(store.Snapshot(), "route")
	if !found {
		t.Fatal("route inventory was not captured")
	}
	return inventory.Manifest
}

func writeRouteFiles(
	t *testing.T,
	root string,
	route string,
	segment int,
	modTime time.Time,
	names ...string,
) {
	t.Helper()
	directory := filepath.Join(root, route+"--"+strconv.Itoa(segment))
	mustMkdir(t, directory)
	for _, name := range names {
		writeAt(t, filepath.Join(directory, name), []byte(name), modTime)
	}
}

func testConfig(root, spool string) config.Config {
	cfg := config.Defaults()
	cfg.ServerURL = "https://example.invalid"
	cfg.DeviceID = "device"
	cfg.Token = "token"
	cfg.Roots = []config.Root{{Name: "realdata", Path: root}}
	cfg.SpoolDir = spool
	cfg.JournalPath = filepath.Join(spool, "journal.json")
	cfg.StableDuration = config.Duration{Duration: time.Second}
	cfg.FinalSegmentGrace = config.Duration{Duration: time.Hour}
	cfg.OffroadSegmentGrace = config.Duration{Duration: time.Second}
	cfg.NonSegmentGrace = config.Duration{Duration: time.Second}
	cfg.MaxFilesPerScan = 8
	return cfg
}

func mustMkdir(t *testing.T, path string) {
	t.Helper()
	if err := os.MkdirAll(path, 0o700); err != nil {
		t.Fatal(err)
	}
}

func writeAt(t *testing.T, path string, contents []byte, modTime time.Time) {
	t.Helper()
	if err := os.WriteFile(path, contents, 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.Chtimes(path, modTime, modTime); err != nil {
		t.Fatal(err)
	}
}

func contains(values []string, target string) bool {
	for _, value := range values {
		if value == target {
			return true
		}
	}
	return false
}

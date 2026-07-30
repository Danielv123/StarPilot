package scanner

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"

	"starpilot.local/comma-companion-agent/internal/config"
	inventorycontract "starpilot.local/comma-companion-agent/internal/inventory"
	"starpilot.local/comma-companion-agent/internal/journal"
	"starpilot.local/comma-companion-agent/internal/state"
)

type Logger interface {
	Printf(format string, values ...any)
}

type candidate struct {
	sourcePath         string
	rootName           string
	relativePath       string
	routeName          string
	segmentNumber      *int
	artifactType       string
	camera             string
	size               int64
	modTimeNS          int64
	segmentDir         string
	hasLock            bool
	rootScanComplete   bool
	completionEvidence []string
	firstObserved      time.Time
}

type Result struct {
	FilesSeen    int
	FilesSpooled int
	BytesSpooled int64
	Errors       int
}

type SuggestedCoverage struct {
	Role            string
	PresentSegments int
	SampledSegments int
}

type StreamSuggestion struct {
	ExpectedStreams []config.InventoryStream
	SampledRoutes   []string
	SampledSegments int
	Coverage        []SuggestedCoverage
}

type Scanner struct {
	config  config.Config
	journal *journal.Store
	logger  Logger
	now     func() time.Time
}

func New(cfg config.Config, store *journal.Store, logger Logger) *Scanner {
	return &Scanner{
		config:  cfg,
		journal: store,
		logger:  logger,
		now:     time.Now,
	}
}

func (s *Scanner) Scan(ctx context.Context, offroad bool) Result {
	now := s.now().UTC()
	all := make([]candidate, 0)
	result := Result{}
	for _, root := range s.config.Roots {
		candidates, errorsFound := walkRoot(ctx, root)
		for index := range candidates {
			candidates[index].rootScanComplete = errorsFound == 0
		}
		all = append(all, candidates...)
		result.Errors += errorsFound
	}
	rootWalksComplete := result.Errors == 0
	result.FilesSeen = len(all)

	if err := s.recordObservations(all, now, result.Errors); err != nil {
		s.logger.Printf("scan: persist observations: %v", err)
		result.Errors++
		return result
	}

	snapshot := s.journal.Snapshot()
	eligible := selectEligible(all, snapshot, now, offroad, s.config)
	if len(eligible) > s.config.MaxFilesPerScan {
		eligible = fairBatch(eligible, s.config.MaxFilesPerScan)
	}
	for _, item := range eligible {
		select {
		case <-ctx.Done():
			return result
		default:
		}
		created, bytes, err := s.spool(item, now)
		if err != nil {
			s.logger.Printf("scan: spool %s: %v", item.sourcePath, err)
			result.Errors++
			continue
		}
		if created {
			result.FilesSpooled++
			result.BytesSpooled += bytes
		}
	}
	if rootWalksComplete {
		if err := s.recordRouteInventories(all, now, offroad); err != nil {
			s.logger.Printf("scan: capture route inventory: %v", err)
			result.Errors++
		}
	}
	return result
}

func fairBatch(eligible []candidate, limit int) []candidate {
	if len(eligible) <= limit {
		return eligible
	}
	selected := append([]candidate(nil), eligible[:limit]...)
	if limit <= 1 {
		return selected
	}
	for _, item := range selected {
		if item.artifactType == "video" {
			return selected
		}
	}
	for _, item := range eligible[limit:] {
		if item.artifactType == "video" {
			selected[limit-1] = item
			break
		}
	}
	return selected
}

func walkRoot(ctx context.Context, root config.Root) ([]candidate, int) {
	var candidates []candidate
	errorCount := 0
	rootInfo, rootErr := os.Lstat(root.Path)
	if errors.Is(rootErr, os.ErrNotExist) {
		// StarPilot selects these roots as alternatives. A mode that has never
		// been active commonly has no directory and contributes no capability.
		return candidates, 0
	}
	if rootErr != nil || !rootInfo.IsDir() || rootInfo.Mode()&os.ModeSymlink != 0 {
		return candidates, 1
	}
	lockDirs := make(map[string]bool)
	err := filepath.WalkDir(root.Path, func(path string, entry fs.DirEntry, walkErr error) error {
		if walkErr != nil {
			errorCount++
			return nil
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		default:
		}
		if entry.Type()&os.ModeSymlink != 0 {
			if entry.IsDir() {
				return filepath.SkipDir
			}
			return nil
		}
		if entry.IsDir() {
			return nil
		}
		base := strings.ToLower(entry.Name())
		if base == ".lock" || strings.HasSuffix(base, ".lock") {
			lockDirs[filepath.Dir(path)] = true
			return nil
		}
		if strings.HasSuffix(base, ".tmp") || strings.HasSuffix(base, ".part") || strings.HasSuffix(base, ".partial") {
			lockDirs[filepath.Dir(path)] = true
			return nil
		}
		info, err := entry.Info()
		if err != nil || !info.Mode().IsRegular() {
			if err != nil {
				errorCount++
			}
			return nil
		}
		relative, err := filepath.Rel(root.Path, path)
		if err != nil || strings.HasPrefix(relative, "..") {
			errorCount++
			return nil
		}
		route, segment, segmentDir := parseSegment(relative)
		artifactType, camera := classify(entry.Name())
		candidates = append(candidates, candidate{
			sourcePath:    path,
			rootName:      root.Name,
			relativePath:  filepath.ToSlash(filepath.Join(root.Name, relative)),
			routeName:     route,
			segmentNumber: segment,
			artifactType:  artifactType,
			camera:        camera,
			size:          info.Size(),
			modTimeNS:     info.ModTime().UnixNano(),
			segmentDir:    filepath.Join(root.Path, segmentDir),
		})
		return nil
	})
	if err != nil {
		errorCount++
	}
	for i := range candidates {
		if candidates[i].segmentNumber == nil {
			continue
		}
		dir := candidates[i].segmentDir
		for {
			if lockDirs[dir] {
				candidates[i].hasLock = true
				break
			}
			if samePath(dir, root.Path) {
				break
			}
			parent := filepath.Dir(dir)
			if parent == dir {
				break
			}
			dir = parent
		}
	}
	return candidates, errorCount
}

func parseSegment(relative string) (string, *int, string) {
	parts := strings.Split(filepath.Clean(relative), string(filepath.Separator))
	for index, part := range parts {
		if route, segment, ok := splitSegmentName(part); ok {
			return route, &segment, filepath.Join(parts[:index+1]...)
		}
	}
	if len(parts) >= 3 {
		if segment, err := strconv.Atoi(parts[len(parts)-2]); err == nil && segment >= 0 {
			route := strings.Join(parts[:len(parts)-2], "--")
			return route, &segment, filepath.Join(parts[:len(parts)-1]...)
		}
	}
	return "", nil, filepath.Dir(relative)
}

func splitSegmentName(name string) (string, int, bool) {
	index := strings.LastIndex(name, "--")
	if index <= 0 || index+2 >= len(name) {
		return "", 0, false
	}
	number, err := strconv.Atoi(name[index+2:])
	if err != nil || number < 0 {
		return "", 0, false
	}
	return name[:index], number, true
}

func classify(name string) (string, string) {
	switch strings.ToLower(name) {
	case "fcamera.hevc":
		return "video", "road"
	case "ecamera.hevc":
		return "video", "wide"
	case "qcamera.ts", "qcamera.hevc":
		return "video", "qcamera"
	case "dcamera.hevc":
		return "video", "driver"
	case "rlog", "rlog.bz2", "rlog.zst":
		return "rlog", ""
	case "qlog", "qlog.bz2", "qlog.zst":
		return "qlog", ""
	default:
		return "artifact", ""
	}
}

func (s *Scanner) recordObservations(items []candidate, now time.Time, scanErrors int) error {
	return s.journal.Update(func(data *state.Journal) error {
		previousScan := data.LastScanAt
		maxGap := 10 * time.Minute
		if configuredGap := 3 * s.config.ScanInterval.Duration; configuredGap > maxGap {
			maxGap = configuredGap
		}
		for _, item := range items {
			previous, found := data.Observations[item.sourcePath]
			stable := 1
			stableSince := now
			gap := now.Sub(previous.LastSeenAt)
			if found && previous.Size == item.size && previous.ModTimeNS == item.modTimeNS &&
				previous.LastSeenAt.Equal(previousScan) && gap >= 0 && gap <= maxGap {
				stable = previous.StableCount + 1
				stableSince = previous.StableSince
				if stableSince.IsZero() {
					stableSince = previous.LastSeenAt
				}
			}
			data.Observations[item.sourcePath] = state.Observation{
				SourcePath:   item.sourcePath,
				RootName:     item.rootName,
				RelativePath: item.relativePath,
				Size:         item.size,
				ModTimeNS:    item.modTimeNS,
				StableCount:  stable,
				StableSince:  stableSince,
				LastSeenAt:   now,
			}
		}
		for path, observation := range data.Observations {
			if now.Sub(observation.LastSeenAt) > 7*24*time.Hour {
				delete(data.Observations, path)
			}
		}
		data.LastScanAt = now
		data.Counters.ScanErrors += int64(scanErrors)
		return nil
	})
}

func selectEligible(items []candidate, snapshot state.Journal, now time.Time, offroad bool, cfg config.Config) []candidate {
	captured := make(map[string]bool, len(snapshot.Files))
	respoolAfter := make(map[string]time.Time)
	for _, file := range snapshot.Files {
		if file.State == state.FileReleased && file.NeedsRespool {
			if !file.ReleasedAt.IsZero() {
				respoolAfter[captureKey(file.SourcePath, file.Size, file.ModTimeNS)] =
					file.ReleasedAt.Add(cfg.ScanInterval.Duration)
			}
			continue
		}
		captured[captureKey(file.SourcePath, file.Size, file.ModTimeNS)] = true
	}
	maxSegment := make(map[string]int)
	for _, item := range items {
		if item.segmentNumber == nil {
			continue
		}
		key := item.rootName + "\x00" + item.routeName
		if current, exists := maxSegment[key]; !exists || *item.segmentNumber > current {
			maxSegment[key] = *item.segmentNumber
		}
	}

	segmentReady := make(map[string]bool)
	for _, item := range items {
		if item.segmentNumber == nil {
			continue
		}
		key := item.rootName + "\x00" + item.routeName + "\x00" + strconv.Itoa(*item.segmentNumber)
		if _, exists := segmentReady[key]; !exists {
			segmentReady[key] = true
		}
		observation := snapshot.Observations[item.sourcePath]
		if !item.rootScanComplete || item.hasLock || observation.StableCount < cfg.StableObservations ||
			now.Sub(observation.StableSince) < cfg.StableDuration.Duration {
			segmentReady[key] = false
		}
	}

	eligible := make([]candidate, 0)
	for _, item := range items {
		observation := snapshot.Observations[item.sourcePath]
		if retryAt, exists := respoolAfter[captureKey(item.sourcePath, item.size, item.modTimeNS)]; exists && now.Before(retryAt) {
			continue
		}
		if !item.rootScanComplete || observation.StableCount < cfg.StableObservations ||
			now.Sub(observation.StableSince) < cfg.StableDuration.Duration ||
			captured[captureKey(item.sourcePath, item.size, item.modTimeNS)] {
			continue
		}
		modTime := time.Unix(0, item.modTimeNS)
		item.firstObserved = observation.StableSince
		if item.segmentNumber == nil {
			if now.Sub(modTime) >= cfg.NonSegmentGrace.Duration {
				item.completionEvidence = []string{"stable_duration", "non_segment_grace"}
				eligible = append(eligible, item)
			}
			continue
		}
		segmentKey := item.rootName + "\x00" + item.routeName + "\x00" + strconv.Itoa(*item.segmentNumber)
		if !segmentReady[segmentKey] {
			continue
		}
		routeKey := item.rootName + "\x00" + item.routeName
		if *item.segmentNumber < maxSegment[routeKey] {
			item.completionEvidence = []string{"no_lock", "stable_duration", "newer_segment"}
			eligible = append(eligible, item)
			continue
		}
		grace := cfg.FinalSegmentGrace.Duration
		if offroad {
			grace = cfg.OffroadSegmentGrace.Duration
		}
		if now.Sub(modTime) >= grace {
			item.completionEvidence = []string{"no_lock", "stable_duration"}
			if offroad {
				item.completionEvidence = append(item.completionEvidence, "offroad_grace")
			} else {
				item.completionEvidence = append(item.completionEvidence, "final_segment_grace")
			}
			eligible = append(eligible, item)
		}
	}
	sort.Slice(eligible, func(i, j int) bool {
		leftPriority := candidatePriority(eligible[i])
		rightPriority := candidatePriority(eligible[j])
		if leftPriority != rightPriority {
			return leftPriority < rightPriority
		}
		if eligible[i].modTimeNS != eligible[j].modTimeNS {
			return eligible[i].modTimeNS < eligible[j].modTimeNS
		}
		return eligible[i].relativePath < eligible[j].relativePath
	})
	return eligible
}

func candidatePriority(item candidate) int {
	switch {
	case item.artifactType == "qlog":
		return 0
	case item.artifactType == "rlog":
		return 1
	case item.camera == "qcamera":
		return 2
	case item.camera == "road":
		return 3
	case item.camera == "wide":
		return 4
	case item.camera == "driver":
		return 5
	default:
		return 6
	}
}

func captureKey(path string, size, modTimeNS int64) string {
	return path + "\x00" + strconv.FormatInt(size, 10) + "\x00" + strconv.FormatInt(modTimeNS, 10)
}

func (s *Scanner) spool(item candidate, now time.Time) (bool, int64, error) {
	sourceInfo, err := os.Stat(item.sourcePath)
	if err != nil {
		return false, 0, err
	}
	if !sourceInfo.Mode().IsRegular() || sourceInfo.Size() != item.size || sourceInfo.ModTime().UnixNano() != item.modTimeNS {
		return false, 0, errors.New("source changed after stable observation")
	}
	id := fileID(item)
	dir := filepath.Join(s.config.SpoolDir, "files", id[:2], id[2:4])
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return false, 0, err
	}
	spoolPath := filepath.Join(dir, id+".data")
	spoolInfo, err := os.Stat(spoolPath)
	if errors.Is(err, fs.ErrNotExist) {
		if err := os.Link(item.sourcePath, spoolPath); err != nil {
			return false, 0, fmt.Errorf("hardlink into spool (spool must share the source filesystem): %w", err)
		}
		spoolInfo, err = os.Stat(spoolPath)
	}
	if err != nil {
		return false, 0, err
	}
	if !os.SameFile(sourceInfo, spoolInfo) {
		_ = os.Remove(spoolPath)
		return false, 0, errors.New("spool collision is not the source hardlink")
	}
	digest, hashedSize, err := hashFile(spoolPath)
	if err != nil {
		return false, 0, err
	}
	sourceAfter, err := os.Stat(item.sourcePath)
	if err != nil || !os.SameFile(sourceAfter, spoolInfo) ||
		sourceAfter.Size() != item.size || sourceAfter.ModTime().UnixNano() != item.modTimeNS ||
		hashedSize != item.size {
		_ = os.Remove(spoolPath)
		return false, 0, errors.New("source changed while hashing")
	}
	file := state.File{
		ID:                 id,
		SourcePath:         item.sourcePath,
		SpoolPath:          spoolPath,
		RootName:           item.rootName,
		RelativePath:       item.relativePath,
		RouteName:          item.routeName,
		SegmentNumber:      item.segmentNumber,
		ArtifactType:       item.artifactType,
		Camera:             item.camera,
		Size:               item.size,
		ModTimeNS:          item.modTimeNS,
		SHA256:             digest,
		CompletionEvidence: append([]string(nil), item.completionEvidence...),
		Partial:            false,
		State:              state.FileSpooled,
		FirstObserved:      item.firstObserved,
		SpooledAt:          now,
	}
	if err := s.journal.Update(func(data *state.Journal) error {
		if existing, ok := data.Files[id]; ok {
			if existing.SHA256 != digest || existing.Size != item.size {
				return errors.New("file ID collision")
			}
			if existing.State != state.FileReleased || !existing.NeedsRespool {
				return nil
			}
			file.FirstObserved = existing.FirstObserved
			file.UploadAttempt = existing.UploadAttempt
			file.AutoRedeclarations = existing.AutoRedeclarations
		}
		data.Files[id] = file
		return nil
	}); err != nil {
		return false, 0, err
	}
	return true, item.size, nil
}

type capturedInventoryFile struct {
	file     state.File
	manifest state.InventoryFile
}

type routeManifestCandidate struct {
	contentSHA256 string
	manifest      state.RouteManifest
}

func (s *Scanner) recordRouteInventories(items []candidate, now time.Time, offroad bool) error {
	snapshot := s.journal.Snapshot()
	captured := make(map[string]state.File, len(snapshot.Files))
	for _, file := range snapshot.Files {
		if file.CancelDeleteRecord || !validSHA256(file.SHA256) {
			continue
		}
		captured[captureKey(file.SourcePath, file.Size, file.ModTimeNS)] = file
	}
	routeItems := make(map[string][]candidate)
	for _, item := range items {
		if item.routeName == "" || item.segmentNumber == nil {
			continue
		}
		routeItems[item.routeName] = append(routeItems[item.routeName], item)
	}
	candidates := make(map[string]routeManifestCandidate)
	for routeName, route := range routeItems {
		candidateManifest, ready, err := s.buildRouteManifest(
			routeName,
			route,
			captured,
			snapshot,
			now,
			offroad,
		)
		if err != nil {
			return err
		}
		if !ready {
			continue
		}
		contentSHA256, err := inventorycontract.ContentSHA256(candidateManifest)
		if err != nil {
			return err
		}
		candidates[routeName] = routeManifestCandidate{
			contentSHA256: contentSHA256,
			manifest:      candidateManifest,
		}
	}
	if len(candidates) == 0 {
		return nil
	}
	routeNames := make([]string, 0, len(candidates))
	for routeName := range candidates {
		routeNames = append(routeNames, routeName)
	}
	sort.Strings(routeNames)
	return s.journal.Update(func(data *state.Journal) error {
		for _, routeName := range routeNames {
			candidateManifest := candidates[routeName]
			latest, found := latestRouteInventory(*data, routeName)
			if found && latest.ContentSHA256 == candidateManifest.contentSHA256 {
				continue
			}
			generation := 1
			var previous *string
			if found {
				if latest.Manifest.Generation >= 1024 {
					return fmt.Errorf("route %s exceeded 1024 inventory generations", routeName)
				}
				generation = latest.Manifest.Generation + 1
				value := latest.ManifestSHA256
				previous = &value
			}
			manifest := candidateManifest.manifest
			manifest.Generation = generation
			manifest.PreviousManifestSHA256 = previous
			manifest.ClosedAt = now
			if err := inventorycontract.ValidateAndNormalize(&manifest); err != nil {
				return fmt.Errorf("validate route %s inventory: %w", routeName, err)
			}
			_, manifestSHA256, err := inventorycontract.CanonicalManifest(manifest)
			if err != nil {
				return err
			}
			if _, exists := data.Inventories[manifestSHA256]; exists {
				continue
			}
			data.Inventories[manifestSHA256] = state.RouteInventory{
				ContentSHA256:  candidateManifest.contentSHA256,
				Manifest:       manifest,
				ManifestSHA256: manifestSHA256,
				CapturedAt:     now,
			}
			data.Counters.InventoriesCaptured++
		}
		return nil
	})
}

func (s *Scanner) buildRouteManifest(
	routeName string,
	items []candidate,
	captured map[string]state.File,
	snapshot state.Journal,
	now time.Time,
	offroad bool,
) (state.RouteManifest, bool, error) {
	maxSegment := -1
	currentPaths := make(map[string]bool)
	filesByPath := make(map[string]state.File)
	for _, item := range items {
		observation, observed := snapshot.Observations[item.sourcePath]
		if !observed || !item.rootScanComplete || item.hasLock ||
			observation.StableCount < s.config.StableObservations ||
			now.Sub(observation.StableSince) < s.config.StableDuration.Duration {
			return state.RouteManifest{}, false, nil
		}
		if *item.segmentNumber > maxSegment {
			maxSegment = *item.segmentNumber
		}
		file, exists := captured[captureKey(item.sourcePath, item.size, item.modTimeNS)]
		if !exists || file.RouteName != routeName || file.SegmentNumber == nil ||
			*file.SegmentNumber != *item.segmentNumber {
			return state.RouteManifest{}, false, nil
		}
		currentPaths[file.RelativePath] = true
		filesByPath[file.RelativePath] = file
	}
	if maxSegment < 0 {
		return state.RouteManifest{}, false, nil
	}
	closureGrace := s.config.FinalSegmentGrace.Duration
	closureEvidence := "final_segment_grace"
	if offroad {
		closureGrace = s.config.OffroadSegmentGrace.Duration
		closureEvidence = "offroad_segment_grace"
	}
	for _, item := range items {
		if *item.segmentNumber == maxSegment {
			age := now.Sub(time.Unix(0, item.modTimeNS))
			if age < 0 || age < closureGrace {
				return state.RouteManifest{}, false, nil
			}
		}
	}
	for _, file := range snapshot.Files {
		if file.RouteName != routeName || file.CancelDeleteRecord ||
			!validSHA256(file.SHA256) || currentPaths[file.RelativePath] {
			continue
		}
		if existing, exists := filesByPath[file.RelativePath]; !exists ||
			file.SpooledAt.After(existing.SpooledAt) {
			filesByPath[file.RelativePath] = file
		}
	}

	sources := make([]capturedInventoryFile, 0, len(filesByPath))
	for _, file := range filesByPath {
		camera := stringPointer(file.Camera)
		sources = append(sources, capturedInventoryFile{
			file: file,
			manifest: state.InventoryFile{
				ArtifactType: file.ArtifactType,
				Camera:       camera,
				MTimeNS:      file.ModTimeNS,
				RelativePath: file.RelativePath,
				SHA256:       strings.ToLower(file.SHA256),
				Size:         file.Size,
			},
		})
	}
	sort.Slice(sources, func(i, j int) bool {
		return sources[i].manifest.RelativePath < sources[j].manifest.RelativePath
	})

	rootNames := make(map[string]bool)
	for _, source := range sources {
		rootNames[source.file.RootName] = true
	}
	expectedByRole := make(map[string]state.ExpectedStream)
	configuredRoots := make(map[string]bool)
	configuredRLogRoots := make(map[string]bool)
	for _, configured := range s.config.Inventory.ExpectedStreams {
		if !rootNames[configured.RootName] {
			continue
		}
		expected := expectedStream(
			configured.RootName,
			configured.ArtifactType,
			configured.Camera,
		)
		expectedByRole[expected.Role] = expected
		configuredRoots[configured.RootName] = true
		if configured.ArtifactType == "rlog" {
			configuredRLogRoots[configured.RootName] = true
		}
	}
	for _, source := range sources {
		if repeatableStream(source.file.ArtifactType) {
			expected := expectedStream(
				source.file.RootName,
				source.file.ArtifactType,
				source.file.Camera,
			)
			expectedByRole[expected.Role] = expected
		}
	}
	allActiveRootsConfigured := len(rootNames) > 0
	allActiveRootsHaveConfiguredRLog := len(rootNames) > 0
	for rootName := range rootNames {
		if !configuredRoots[rootName] {
			allActiveRootsConfigured = false
		}
		if !configuredRLogRoots[rootName] {
			allActiveRootsHaveConfiguredRLog = false
		}
	}
	expectedStreams := make([]state.ExpectedStream, 0, len(expectedByRole))
	for _, expected := range expectedByRole {
		expectedStreams = append(expectedStreams, expected)
	}
	sort.Slice(expectedStreams, func(i, j int) bool {
		return expectedStreams[i].Role < expectedStreams[j].Role
	})

	segmentFiles := make(map[int][]capturedInventoryFile)
	routeFiles := make([]state.InventoryFile, 0)
	segmentNumbers := make(map[int]bool)
	for _, source := range sources {
		if source.file.SegmentNumber == nil {
			routeFiles = append(routeFiles, source.manifest)
			continue
		}
		number := *source.file.SegmentNumber
		segmentNumbers[number] = true
		segmentFiles[number] = append(segmentFiles[number], source)
		if number > maxSegment {
			maxSegment = number
		}
	}
	missingSegments := make([]int, 0)
	for number := 0; number <= maxSegment; number++ {
		if !segmentNumbers[number] {
			missingSegments = append(missingSegments, number)
		}
	}
	segments := make([]state.InventorySegment, 0, len(segmentNumbers))
	missingStreams := false
	for number := 0; number <= maxSegment; number++ {
		if !segmentNumbers[number] {
			continue
		}
		files := segmentFiles[number]
		sort.Slice(files, func(i, j int) bool {
			return files[i].manifest.RelativePath < files[j].manifest.RelativePath
		})
		manifestFiles := make([]state.InventoryFile, 0, len(files))
		roleFiles := make(map[string]capturedInventoryFile)
		for _, file := range files {
			manifestFiles = append(manifestFiles, file.manifest)
			if repeatableStream(file.file.ArtifactType) {
				role := inventorycontract.StreamRole(
					file.file.RootName,
					file.file.ArtifactType,
					file.file.Camera,
				)
				if _, exists := roleFiles[role]; !exists {
					roleFiles[role] = file
				}
			}
		}
		streams := make([]state.InventoryStream, 0, len(expectedStreams))
		for _, expected := range expectedStreams {
			if file, exists := roleFiles[expected.Role]; exists {
				relativePath := file.manifest.RelativePath
				mtime := file.manifest.MTimeNS
				digest := file.manifest.SHA256
				size := file.manifest.Size
				streams = append(streams, state.InventoryStream{
					MTimeNS:      &mtime,
					RelativePath: &relativePath,
					Role:         expected.Role,
					SHA256:       &digest,
					Size:         &size,
					Status:       "present",
				})
			} else {
				missingStreams = true
				streams = append(streams, state.InventoryStream{
					Role:   expected.Role,
					Status: "missing",
				})
			}
		}
		segments = append(segments, state.InventorySegment{
			Files:   manifestFiles,
			Number:  number,
			Streams: streams,
		})
	}
	rootList := make([]string, 0, len(rootNames))
	for rootName := range rootNames {
		rootList = append(rootList, rootName)
	}
	sort.Strings(rootList)
	evidence := []string{
		"no_lock",
		"stable_duration",
		closureEvidence,
	}
	if offroad {
		evidence = append(evidence, "offroad")
	}
	inventoryState := "complete"
	capabilitySource := "configured+route_union"
	if len(rootNames) > 1 {
		inventoryState = "partial"
		evidence = append(evidence, "multiple_active_log_roots")
	}
	if !allActiveRootsConfigured {
		inventoryState = "partial"
		capabilitySource = "route_union_unconfigured"
		evidence = append(evidence, "expected_streams_unconfigured")
		evidence = append(evidence, "active_root_expected_streams_unconfigured")
	}
	if !allActiveRootsHaveConfiguredRLog {
		inventoryState = "partial"
		evidence = append(evidence, "rlog_stream_unconfigured")
	}
	if len(missingSegments) > 0 {
		inventoryState = "partial"
		evidence = append(evidence, "missing_segment_numbers")
	}
	if missingStreams {
		inventoryState = "partial"
		evidence = append(evidence, "missing_expected_streams")
	}
	manifest := state.RouteManifest{
		CapabilitySource:       capabilitySource,
		ClosedAt:               now,
		ClosureEvidence:        evidence,
		ExpectedStreams:        expectedStreams,
		Generation:             1,
		MissingSegmentNumbers:  missingSegments,
		PreviousManifestSHA256: nil,
		RootNames:              rootList,
		RouteClosed:            true,
		RouteFiles:             routeFiles,
		RouteName:              routeName,
		Schema:                 inventorycontract.Schema,
		SchemaVersion:          inventorycontract.SchemaVersion,
		Segments:               segments,
		State:                  inventoryState,
	}
	if err := inventorycontract.ValidateAndNormalize(&manifest); err != nil {
		return state.RouteManifest{}, false, err
	}
	return manifest, true, nil
}

func latestRouteInventory(
	data state.Journal,
	routeName string,
) (state.RouteInventory, bool) {
	var latest state.RouteInventory
	found := false
	for _, record := range data.Inventories {
		if record.Manifest.RouteName != routeName {
			continue
		}
		if !found || record.Manifest.Generation > latest.Manifest.Generation {
			latest = record
			found = true
		}
	}
	return latest, found
}

func expectedStream(rootName, artifactType, camera string) state.ExpectedStream {
	return state.ExpectedStream{
		ArtifactType: artifactType,
		Camera:       stringPointer(camera),
		Role:         inventorycontract.StreamRole(rootName, artifactType, camera),
		RootName:     rootName,
	}
}

func stringPointer(value string) *string {
	if value == "" {
		return nil
	}
	result := value
	return &result
}

func repeatableStream(artifactType string) bool {
	switch artifactType {
	case "video", "rlog", "qlog":
		return true
	default:
		return false
	}
}

func validSHA256(value string) bool {
	if len(value) != 64 {
		return false
	}
	_, err := hex.DecodeString(value)
	return err == nil
}

func SuggestExpectedStreams(
	ctx context.Context,
	cfg config.Config,
	sampleLimit int,
	now time.Time,
) (StreamSuggestion, error) {
	if sampleLimit < 2 || sampleLimit > 128 {
		return StreamSuggestion{}, errors.New("suggestion sample limit must be between 2 and 128")
	}
	if now.IsZero() {
		now = time.Now()
	}
	now = now.UTC()
	all := make([]candidate, 0)
	for _, root := range cfg.Roots {
		items, errorCount := walkRoot(ctx, root)
		if errorCount != 0 {
			return StreamSuggestion{}, fmt.Errorf(
				"logging root %s could not be scanned completely (%d errors)",
				root.Name,
				errorCount,
			)
		}
		all = append(all, items...)
	}
	type segmentSample struct {
		route       string
		number      int
		newestMTime int64
		items       []candidate
	}
	segmentByKey := make(map[string]*segmentSample)
	maxSegmentByRoute := make(map[string]int)
	for _, item := range all {
		if item.routeName == "" || item.segmentNumber == nil {
			continue
		}
		key := item.routeName + "\x00" + strconv.Itoa(*item.segmentNumber)
		sample, exists := segmentByKey[key]
		if !exists {
			sample = &segmentSample{
				route:  item.routeName,
				number: *item.segmentNumber,
			}
			segmentByKey[key] = sample
		}
		sample.items = append(sample.items, item)
		if item.modTimeNS > sample.newestMTime {
			sample.newestMTime = item.modTimeNS
		}
		if current, exists := maxSegmentByRoute[item.routeName]; !exists ||
			*item.segmentNumber > current {
			maxSegmentByRoute[item.routeName] = *item.segmentNumber
		}
	}
	closed := make([]segmentSample, 0, len(segmentByKey))
	for _, sample := range segmentByKey {
		stable := len(sample.items) > 0
		for _, item := range sample.items {
			age := now.Sub(time.Unix(0, item.modTimeNS))
			if item.hasLock || age < 0 || age < cfg.StableDuration.Duration {
				stable = false
				break
			}
		}
		if !stable {
			continue
		}
		if sample.number == maxSegmentByRoute[sample.route] {
			age := now.Sub(time.Unix(0, sample.newestMTime))
			if age < 0 || age < cfg.OffroadSegmentGrace.Duration {
				continue
			}
		}
		closed = append(closed, *sample)
	}
	sort.Slice(closed, func(i, j int) bool {
		if closed[i].newestMTime != closed[j].newestMTime {
			return closed[i].newestMTime > closed[j].newestMTime
		}
		if closed[i].route != closed[j].route {
			return closed[i].route < closed[j].route
		}
		return closed[i].number > closed[j].number
	})
	if len(closed) > sampleLimit {
		closed = closed[:sampleLimit]
	}
	if len(closed) < 2 {
		return StreamSuggestion{}, errors.New(
			"fewer than two closed, stable segments are available for capability discovery",
		)
	}
	roleStreams := make(map[string]config.InventoryStream)
	roleCoverage := make(map[string]int)
	routes := make(map[string]bool)
	for _, sample := range closed {
		routes[sample.route] = true
		present := make(map[string]bool)
		for _, item := range sample.items {
			if !repeatableStream(item.artifactType) {
				continue
			}
			role := inventorycontract.StreamRole(item.rootName, item.artifactType, item.camera)
			roleStreams[role] = config.InventoryStream{
				RootName:     item.rootName,
				ArtifactType: item.artifactType,
				Camera:       item.camera,
			}
			present[role] = true
		}
		for role := range present {
			roleCoverage[role]++
		}
	}
	if len(roleStreams) == 0 {
		return StreamSuggestion{}, errors.New("sampled segments contain no recognized video or log streams")
	}
	profileRoots := make(map[string]bool)
	rlogRoots := make(map[string]bool)
	for _, stream := range roleStreams {
		profileRoots[stream.RootName] = true
		if stream.ArtifactType == "rlog" {
			rlogRoots[stream.RootName] = true
		}
	}
	for rootName := range profileRoots {
		if !rlogRoots[rootName] {
			return StreamSuggestion{}, fmt.Errorf(
				"sampled root %s has no rlog; it cannot form an archive-ready profile",
				rootName,
			)
		}
	}
	roles := make([]string, 0, len(roleStreams))
	for role := range roleStreams {
		roles = append(roles, role)
	}
	sort.Strings(roles)
	result := StreamSuggestion{
		ExpectedStreams: make([]config.InventoryStream, 0, len(roles)),
		SampledSegments: len(closed),
		Coverage:        make([]SuggestedCoverage, 0, len(roles)),
	}
	for _, role := range roles {
		result.ExpectedStreams = append(result.ExpectedStreams, roleStreams[role])
		result.Coverage = append(result.Coverage, SuggestedCoverage{
			Role:            role,
			PresentSegments: roleCoverage[role],
			SampledSegments: len(closed),
		})
	}
	for route := range routes {
		result.SampledRoutes = append(result.SampledRoutes, route)
	}
	sort.Strings(result.SampledRoutes)
	return result, nil
}

func fileID(item candidate) string {
	hash := sha256.New()
	_, _ = io.WriteString(hash, item.rootName)
	_, _ = io.WriteString(hash, "\x00")
	_, _ = io.WriteString(hash, item.relativePath)
	_, _ = io.WriteString(hash, "\x00")
	_, _ = io.WriteString(hash, strconv.FormatInt(item.size, 10))
	_, _ = io.WriteString(hash, "\x00")
	_, _ = io.WriteString(hash, strconv.FormatInt(item.modTimeNS, 10))
	return hex.EncodeToString(hash.Sum(nil))
}

func hashFile(path string) (string, int64, error) {
	file, err := os.Open(path)
	if err != nil {
		return "", 0, err
	}
	defer file.Close()
	hash := sha256.New()
	size, err := io.Copy(hash, file)
	if err != nil {
		return "", 0, err
	}
	return hex.EncodeToString(hash.Sum(nil)), size, nil
}

func samePath(left, right string) bool {
	leftAbs, leftErr := filepath.Abs(left)
	rightAbs, rightErr := filepath.Abs(right)
	if leftErr != nil || rightErr != nil {
		return filepath.Clean(left) == filepath.Clean(right)
	}
	return filepath.Clean(leftAbs) == filepath.Clean(rightAbs)
}

package inventory

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"path"
	"sort"
	"strconv"
	"strings"
	"time"

	"starpilot.local/comma-companion-agent/internal/state"
)

const (
	Schema        = "comma-companion.route-inventory"
	SchemaVersion = 1
)

func CanonicalManifest(manifest state.RouteManifest) ([]byte, string, error) {
	encoded, err := json.Marshal(manifest)
	if err != nil {
		return nil, "", fmt.Errorf("encode route manifest: %w", err)
	}
	decoder := json.NewDecoder(bytes.NewReader(encoded))
	decoder.UseNumber()
	var document any
	if err := decoder.Decode(&document); err != nil {
		return nil, "", fmt.Errorf("normalize route manifest: %w", err)
	}
	canonical, err := json.Marshal(document)
	if err != nil {
		return nil, "", fmt.Errorf("canonicalize route manifest: %w", err)
	}
	digest := sha256.Sum256(canonical)
	return canonical, hex.EncodeToString(digest[:]), nil
}

func ContentSHA256(manifest state.RouteManifest) (string, error) {
	manifest.ClosedAt = time.Time{}
	manifest.Generation = 0
	manifest.PreviousManifestSHA256 = nil
	_, digest, err := CanonicalManifest(manifest)
	return digest, err
}

func ValidateAndNormalize(manifest *state.RouteManifest) error {
	if manifest.Schema != Schema || manifest.SchemaVersion != SchemaVersion {
		return errors.New("unsupported route inventory schema")
	}
	if strings.TrimSpace(manifest.RouteName) == "" {
		return errors.New("route inventory route_name is required")
	}
	if manifest.RouteName != strings.TrimSpace(manifest.RouteName) ||
		len(manifest.RouteName) > 256 ||
		strings.ContainsAny(manifest.RouteName, "/\\\x00") ||
		containsControl(manifest.RouteName) {
		return errors.New("route inventory route_name is not a canonical route component")
	}
	if !manifest.RouteClosed {
		return errors.New("route inventory must assert route_closed")
	}
	if manifest.Generation <= 0 || manifest.Generation > 1_000_000 {
		return errors.New("route inventory generation must be positive")
	}
	if manifest.ClosedAt.IsZero() {
		return errors.New("route inventory closed_at is required")
	}
	manifest.ClosedAt = manifest.ClosedAt.UTC()
	if manifest.State != "complete" && manifest.State != "partial" {
		return errors.New("route inventory state must be complete or partial")
	}
	if manifest.CapabilitySource != "configured+route_union" &&
		manifest.CapabilitySource != "route_union_unconfigured" {
		return errors.New("route inventory has an unsupported capability_source")
	}
	if len(manifest.ClosureEvidence) == 0 || len(manifest.ClosureEvidence) > 32 {
		return errors.New("route inventory closure_evidence is required")
	}
	sort.Strings(manifest.RootNames)
	manifest.RootNames = uniqueStrings(manifest.RootNames)
	if len(manifest.RootNames) == 0 || len(manifest.RootNames) > 32 {
		return errors.New("route inventory root_names is required")
	}
	rootNames := make(map[string]bool, len(manifest.RootNames))
	for _, rootName := range manifest.RootNames {
		if !validComponent(rootName) || len(rootName) > 64 {
			return fmt.Errorf("invalid route inventory root name %q", rootName)
		}
		rootNames[rootName] = true
	}
	switch {
	case manifest.Generation == 1 && manifest.PreviousManifestSHA256 != nil:
		return errors.New("first route inventory generation must not have a predecessor")
	case manifest.Generation > 1 && (manifest.PreviousManifestSHA256 == nil ||
		!validLowerSHA256(*manifest.PreviousManifestSHA256)):
		return errors.New("superseding route inventory generation requires a lowercase predecessor digest")
	}
	sort.Strings(manifest.ClosureEvidence)
	manifest.ClosureEvidence = uniqueStrings(manifest.ClosureEvidence)
	for _, evidence := range manifest.ClosureEvidence {
		if !validLooseComponent(evidence) || len(evidence) > 128 {
			return fmt.Errorf("invalid route inventory closure evidence %q", evidence)
		}
	}
	sort.Ints(manifest.MissingSegmentNumbers)
	manifest.MissingSegmentNumbers = uniqueInts(manifest.MissingSegmentNumbers)
	if len(manifest.MissingSegmentNumbers) > 4096 {
		return errors.New("route inventory contains too many missing segment numbers")
	}
	sort.Slice(manifest.ExpectedStreams, func(i, j int) bool {
		return manifest.ExpectedStreams[i].Role < manifest.ExpectedStreams[j].Role
	})
	if len(manifest.ExpectedStreams) > 64 {
		return errors.New("route inventory contains too many expected streams")
	}
	roles := make(map[string]state.ExpectedStream, len(manifest.ExpectedStreams))
	rlogRoots := make(map[string]bool)
	rlogAuthorities := 0
	for _, stream := range manifest.ExpectedStreams {
		if !rootNames[stream.RootName] {
			return fmt.Errorf("expected stream role %q names an undeclared root", stream.Role)
		}
		if err := validateStreamIdentity(
			stream.ArtifactType,
			stream.Camera,
			true,
		); err != nil {
			return fmt.Errorf("expected stream role %q: %w", stream.Role, err)
		}
		expectedRole := StreamRole(stream.RootName, stream.ArtifactType, pointerValue(stream.Camera))
		if stream.Role != expectedRole || len(stream.Role) > 256 {
			return fmt.Errorf("non-canonical stream role %q, expected %q", stream.Role, expectedRole)
		}
		if _, exists := roles[stream.Role]; exists {
			return fmt.Errorf("duplicate expected stream role %q", stream.Role)
		}
		roles[stream.Role] = stream
		if stream.ArtifactType == "rlog" {
			rlogRoots[stream.RootName] = true
			rlogAuthorities++
		}
	}
	sortInventoryFiles(manifest.RouteFiles)
	if len(manifest.RouteFiles) > 4096 {
		return errors.New("route inventory contains too many route files")
	}
	paths := make(map[string]state.InventoryFile)
	if err := addUniqueFiles(paths, manifest.RouteFiles); err != nil {
		return err
	}
	sort.Slice(manifest.Segments, func(i, j int) bool {
		return manifest.Segments[i].Number < manifest.Segments[j].Number
	})
	if len(manifest.Segments) == 0 || len(manifest.Segments) > 4096 {
		return errors.New("route inventory must contain at least one captured segment")
	}
	segmentNumbers := make(map[int]bool, len(manifest.Segments))
	for index := range manifest.Segments {
		segment := &manifest.Segments[index]
		if segment.Number < 0 || segment.Number > 1_000_000 ||
			segmentNumbers[segment.Number] {
			return fmt.Errorf("duplicate or invalid segment number %d", segment.Number)
		}
		segmentNumbers[segment.Number] = true
		sortInventoryFiles(segment.Files)
		if len(segment.Files) > 512 {
			return fmt.Errorf("segment %d contains too many files", segment.Number)
		}
		if err := addUniqueFiles(paths, segment.Files); err != nil {
			return err
		}
		for _, file := range segment.Files {
			routeName, segmentNumber, parsed := parseSegmentPath(file.RelativePath)
			if !parsed || routeName != manifest.RouteName || segmentNumber != segment.Number {
				return fmt.Errorf(
					"segment %d contains file %q from another route or segment",
					segment.Number,
					file.RelativePath,
				)
			}
		}
		sort.Slice(segment.Streams, func(i, j int) bool {
			return segment.Streams[i].Role < segment.Streams[j].Role
		})
		if len(segment.Streams) > 64 || len(segment.Streams) != len(roles) {
			return fmt.Errorf(
				"segment %d has %d stream rows, expected %d",
				segment.Number,
				len(segment.Streams),
				len(roles),
			)
		}
		seenRoles := make(map[string]bool, len(segment.Streams))
		filesByPath := make(map[string]state.InventoryFile, len(segment.Files))
		for _, file := range segment.Files {
			filesByPath[file.RelativePath] = file
		}
		for _, stream := range segment.Streams {
			if _, exists := roles[stream.Role]; !exists || seenRoles[stream.Role] {
				return fmt.Errorf("segment %d has invalid stream role %q", segment.Number, stream.Role)
			}
			seenRoles[stream.Role] = true
			switch stream.Status {
			case "missing":
				if stream.RelativePath != nil || stream.MTimeNS != nil ||
					stream.SHA256 != nil || stream.Size != nil {
					return fmt.Errorf("missing stream %q contains file fields", stream.Role)
				}
			case "present":
				if stream.RelativePath == nil || stream.MTimeNS == nil ||
					stream.SHA256 == nil || stream.Size == nil {
					return fmt.Errorf("present stream %q lacks file fields", stream.Role)
				}
				file, exists := filesByPath[*stream.RelativePath]
				if !exists || file.MTimeNS != *stream.MTimeNS ||
					file.SHA256 != *stream.SHA256 || file.Size != *stream.Size {
					return fmt.Errorf("present stream %q does not match an actual file", stream.Role)
				}
				expected := roles[stream.Role]
				if StreamRole(
					rootFromRelative(file.RelativePath),
					file.ArtifactType,
					pointerValue(file.Camera),
				) != expected.Role {
					return fmt.Errorf("present stream %q references the wrong artifact role", stream.Role)
				}
			default:
				return fmt.Errorf("stream %q has invalid status %q", stream.Role, stream.Status)
			}
		}
	}
	maxSegment := manifest.Segments[len(manifest.Segments)-1].Number
	missingNumbers := make(map[int]bool, len(manifest.MissingSegmentNumbers))
	for _, number := range manifest.MissingSegmentNumbers {
		if number < 0 || number > maxSegment || segmentNumbers[number] {
			return fmt.Errorf("invalid missing segment number %d", number)
		}
		missingNumbers[number] = true
	}
	for number := 0; number <= maxSegment; number++ {
		if segmentNumbers[number] == missingNumbers[number] {
			return fmt.Errorf("segment number %d is neither uniquely present nor missing", number)
		}
	}
	for relativePath := range paths {
		if !rootNames[rootFromRelative(relativePath)] {
			return fmt.Errorf("inventory file %q names an undeclared root", relativePath)
		}
	}
	if len(rootNames) > 1 &&
		(manifest.State != "partial" ||
			!containsString(manifest.ClosureEvidence, "multiple_active_log_roots")) {
		return errors.New(
			"inventory with multiple active logging roots must be partial and identify multiple_active_log_roots",
		)
	}
	if manifest.State == "complete" {
		if len(manifest.ExpectedStreams) == 0 || len(manifest.MissingSegmentNumbers) > 0 {
			return errors.New("complete inventory lacks configured streams or has segment gaps")
		}
		if manifest.CapabilitySource != "configured+route_union" {
			return errors.New("complete inventory lacks an explicit configured capability source")
		}
		if len(rootNames) != 1 || len(rlogRoots) != 1 || rlogAuthorities != 1 {
			return errors.New(
				"complete inventory requires exactly one active logging root and one expected rlog authority",
			)
		}
		for _, segment := range manifest.Segments {
			for _, stream := range segment.Streams {
				if stream.Status != "present" {
					return errors.New("complete inventory contains a missing stream")
				}
			}
		}
	}
	if manifest.CapabilitySource == "route_union_unconfigured" &&
		(manifest.State != "partial" ||
			!containsString(manifest.ClosureEvidence, "expected_streams_unconfigured")) {
		return errors.New(
			"unconfigured route inventory must be partial and identify expected_streams_unconfigured",
		)
	}
	return nil
}

func StreamRole(rootName, artifactType, camera string) string {
	cameraPart := camera
	if cameraPart == "" {
		cameraPart = "-"
	}
	return rootName + "|" + artifactType + "|" + cameraPart
}

func sortInventoryFiles(files []state.InventoryFile) {
	sort.Slice(files, func(i, j int) bool {
		return files[i].RelativePath < files[j].RelativePath
	})
}

func addUniqueFiles(
	paths map[string]state.InventoryFile,
	files []state.InventoryFile,
) error {
	for _, file := range files {
		if !validRelativePath(file.RelativePath) || file.Size < 0 ||
			file.Size > 1<<40 || file.MTimeNS <= 0 || !validLowerSHA256(file.SHA256) {
			return fmt.Errorf("invalid inventory file %q", file.RelativePath)
		}
		if err := validateStreamIdentity(file.ArtifactType, file.Camera, false); err != nil {
			return fmt.Errorf("invalid inventory file %q: %w", file.RelativePath, err)
		}
		if _, exists := paths[file.RelativePath]; exists {
			return fmt.Errorf("duplicate inventory path %q", file.RelativePath)
		}
		paths[file.RelativePath] = file
	}
	return nil
}

func validateStreamIdentity(artifactType string, camera *string, expected bool) error {
	if !validComponent(artifactType) || len(artifactType) > 64 {
		return errors.New("artifact type is not a canonical component")
	}
	switch artifactType {
	case "video":
		if camera == nil || !validComponent(*camera) || len(*camera) > 32 {
			return errors.New("video artifact requires a camera")
		}
	case "rlog", "qlog":
		if camera != nil {
			return errors.New("log artifact must have a null camera")
		}
	default:
		if expected {
			return errors.New("expected stream type must be video, rlog, or qlog")
		}
		if artifactType != "artifact" || camera != nil {
			return errors.New("non-stream file must use artifact with a null camera")
		}
	}
	return nil
}

func validRelativePath(value string) bool {
	if value == "" || len([]byte(value)) > 4096 ||
		strings.Contains(value, "\\") || strings.HasPrefix(value, "/") ||
		strings.ContainsRune(value, '\x00') {
		return false
	}
	clean := path.Clean(value)
	if clean != value || clean == "." || clean == ".." || strings.HasPrefix(clean, "../") {
		return false
	}
	root, _, found := strings.Cut(value, "/")
	return found && validComponent(root)
}

func validComponent(value string) bool {
	if value == "" {
		return false
	}
	for _, character := range value {
		if (character < 'a' || character > 'z') &&
			(character < 'A' || character > 'Z') &&
			(character < '0' || character > '9') &&
			character != '.' && character != '_' && character != '-' {
			return false
		}
	}
	return true
}

func validLooseComponent(value string) bool {
	return value != "" &&
		!strings.ContainsAny(value, "|/\\\x00") &&
		!containsControl(value)
}

func containsControl(value string) bool {
	for _, character := range value {
		if character < 32 || character == 127 {
			return true
		}
	}
	return false
}

func validLowerSHA256(value string) bool {
	if len(value) != sha256.Size*2 || value != strings.ToLower(value) {
		return false
	}
	_, err := hex.DecodeString(value)
	return err == nil
}

func parseSegmentPath(relativePath string) (string, int, bool) {
	parts := strings.Split(relativePath, "/")
	if len(parts) < 2 {
		return "", 0, false
	}
	parts = parts[1:]
	for _, part := range parts {
		index := strings.LastIndex(part, "--")
		if index <= 0 || index+2 >= len(part) {
			continue
		}
		number, err := strconv.Atoi(part[index+2:])
		if err == nil && number >= 0 {
			return part[:index], number, true
		}
	}
	if len(parts) >= 3 {
		number, err := strconv.Atoi(parts[len(parts)-2])
		if err == nil && number >= 0 {
			return strings.Join(parts[:len(parts)-2], "--"), number, true
		}
	}
	return "", 0, false
}

func rootFromRelative(relative string) string {
	if before, _, ok := strings.Cut(strings.ReplaceAll(relative, "\\", "/"), "/"); ok {
		return before
	}
	return relative
}

func containsString(values []string, target string) bool {
	for _, value := range values {
		if value == target {
			return true
		}
	}
	return false
}

func pointerValue(value *string) string {
	if value == nil {
		return ""
	}
	return *value
}

func uniqueStrings(values []string) []string {
	if len(values) == 0 {
		return values
	}
	output := values[:1]
	for _, value := range values[1:] {
		if value != output[len(output)-1] {
			output = append(output, value)
		}
	}
	return output
}

func uniqueInts(values []int) []int {
	if len(values) == 0 {
		return values
	}
	output := values[:1]
	for _, value := range values[1:] {
		if value != output[len(output)-1] {
			output = append(output, value)
		}
	}
	return output
}

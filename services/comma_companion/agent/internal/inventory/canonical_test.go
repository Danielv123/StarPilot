package inventory

import (
	"bytes"
	"strings"
	"testing"
	"time"

	"starpilot.local/comma-companion-agent/internal/state"
)

func TestCanonicalManifestIsDeterministicAfterArrayNormalization(t *testing.T) {
	left := validManifest()
	video := state.ExpectedStream{
		ArtifactType: "video",
		Camera:       stringTestPointer("road"),
		Role:         "realdata|video|road",
		RootName:     "realdata",
	}
	videoFile := state.InventoryFile{
		ArtifactType: "video",
		Camera:       stringTestPointer("road"),
		MTimeNS:      200,
		RelativePath: "realdata/route--0/fcamera.hevc",
		SHA256:       strings.Repeat("b", 64),
		Size:         20,
	}
	left.ExpectedStreams = append([]state.ExpectedStream{video}, left.ExpectedStreams...)
	left.Segments[0].Files = append([]state.InventoryFile{videoFile}, left.Segments[0].Files...)
	left.Segments[0].Streams = append(
		[]state.InventoryStream{presentStream(video.Role, videoFile)},
		left.Segments[0].Streams...,
	)
	right := left
	right.ExpectedStreams = append([]state.ExpectedStream(nil), left.ExpectedStreams...)
	right.Segments = append([]state.InventorySegment(nil), left.Segments...)
	right.Segments[0].Files = append([]state.InventoryFile(nil), left.Segments[0].Files...)
	right.Segments[0].Streams = append([]state.InventoryStream(nil), left.Segments[0].Streams...)
	reverseExpected(right.ExpectedStreams)
	reverseFiles(right.Segments[0].Files)
	reverseStreams(right.Segments[0].Streams)

	if err := ValidateAndNormalize(&left); err != nil {
		t.Fatal(err)
	}
	if err := ValidateAndNormalize(&right); err != nil {
		t.Fatal(err)
	}
	leftJSON, leftDigest, err := CanonicalManifest(left)
	if err != nil {
		t.Fatal(err)
	}
	rightJSON, rightDigest, err := CanonicalManifest(right)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(leftJSON, rightJSON) || leftDigest != rightDigest {
		t.Fatal("semantically identical normalized manifests did not canonicalize identically")
	}
	if bytes.ContainsAny(leftJSON, " \r\n\t") {
		t.Fatalf("canonical JSON contains whitespace: %s", leftJSON)
	}
	if !bytes.HasPrefix(leftJSON, []byte(`{"capability_source":`)) {
		t.Fatalf("canonical object keys are not lexical: %s", leftJSON)
	}
}

func TestCompleteManifestRequiresRootSpecificRLog(t *testing.T) {
	manifest := validManifest()
	videoFile := state.InventoryFile{
		ArtifactType: "video",
		Camera:       stringTestPointer("road"),
		MTimeNS:      200,
		RelativePath: "realdata/route--0/fcamera.hevc",
		SHA256:       strings.Repeat("b", 64),
		Size:         20,
	}
	manifest.ExpectedStreams = []state.ExpectedStream{{
		ArtifactType: "video",
		Camera:       stringTestPointer("road"),
		Role:         "realdata|video|road",
		RootName:     "realdata",
	}}
	manifest.Segments[0].Files = []state.InventoryFile{videoFile}
	manifest.Segments[0].Streams = []state.InventoryStream{
		presentStream("realdata|video|road", videoFile),
	}
	if err := ValidateAndNormalize(&manifest); err == nil ||
		!strings.Contains(err.Error(), "rlog") {
		t.Fatalf("expected complete manifest without rlog to fail, got %v", err)
	}

	manifest.State = "partial"
	manifest.ClosureEvidence = append(manifest.ClosureEvidence, "rlog_stream_unconfigured")
	if err := ValidateAndNormalize(&manifest); err != nil {
		t.Fatalf("partial manifest without configured rlog should remain reportable: %v", err)
	}
}

func TestMultipleActiveLoggingRootsRequirePartialConflictEvidence(t *testing.T) {
	manifest := validManifest()
	hdFile := state.InventoryFile{
		ArtifactType: "rlog",
		MTimeNS:      200,
		RelativePath: "realdata_HD/route--0/rlog.zst",
		SHA256:       strings.Repeat("b", 64),
		Size:         20,
	}
	manifest.RootNames = append(manifest.RootNames, "realdata_HD")
	manifest.ExpectedStreams = append(manifest.ExpectedStreams, state.ExpectedStream{
		ArtifactType: "rlog",
		Role:         "realdata_HD|rlog|-",
		RootName:     "realdata_HD",
	})
	manifest.Segments[0].Files = append(manifest.Segments[0].Files, hdFile)
	manifest.Segments[0].Streams = append(
		manifest.Segments[0].Streams,
		presentStream("realdata_HD|rlog|-", hdFile),
	)

	if err := ValidateAndNormalize(&manifest); err == nil ||
		!strings.Contains(err.Error(), "multiple active logging roots") {
		t.Fatalf("complete multi-root manifest was accepted: %v", err)
	}

	manifest.State = "partial"
	if err := ValidateAndNormalize(&manifest); err == nil ||
		!strings.Contains(err.Error(), "multiple_active_log_roots") {
		t.Fatalf("multi-root manifest without conflict evidence was accepted: %v", err)
	}

	manifest.ClosureEvidence = append(
		manifest.ClosureEvidence,
		"multiple_active_log_roots",
	)
	if err := ValidateAndNormalize(&manifest); err != nil {
		t.Fatalf("explicit partial multi-root manifest should be reportable: %v", err)
	}
}

func TestPartialUnconfiguredCapabilityManifestIsValid(t *testing.T) {
	manifest := validManifest()
	manifest.State = "partial"
	manifest.CapabilitySource = "route_union_unconfigured"
	manifest.ClosureEvidence = append(
		manifest.ClosureEvidence,
		"expected_streams_unconfigured",
		"active_root_expected_streams_unconfigured",
	)
	if err := ValidateAndNormalize(&manifest); err != nil {
		t.Fatal(err)
	}
}

func TestManifestValidationRejectsBrokenClosureAndFileIdentity(t *testing.T) {
	tests := []struct {
		name   string
		mutate func(*state.RouteManifest)
		match  string
	}{
		{
			name: "generation one predecessor",
			mutate: func(manifest *state.RouteManifest) {
				manifest.PreviousManifestSHA256 = stringTestPointer(strings.Repeat("c", 64))
			},
			match: "first route inventory generation",
		},
		{
			name: "generation two lacks predecessor",
			mutate: func(manifest *state.RouteManifest) {
				manifest.Generation = 2
			},
			match: "requires a lowercase predecessor",
		},
		{
			name: "uppercase sha",
			mutate: func(manifest *state.RouteManifest) {
				manifest.Segments[0].Files[0].SHA256 = strings.Repeat("A", 64)
				*manifest.Segments[0].Streams[0].SHA256 = strings.Repeat("A", 64)
			},
			match: "invalid inventory file",
		},
		{
			name: "traversal path",
			mutate: func(manifest *state.RouteManifest) {
				manifest.Segments[0].Files[0].RelativePath = "realdata/../secret"
				*manifest.Segments[0].Streams[0].RelativePath = "realdata/../secret"
			},
			match: "invalid inventory file",
		},
		{
			name: "undeclared gap",
			mutate: func(manifest *state.RouteManifest) {
				segment := manifest.Segments[0]
				segment.Number = 1
				segment.Files[0].RelativePath = "realdata/route--1/rlog.zst"
				*segment.Streams[0].RelativePath = "realdata/route--1/rlog.zst"
				manifest.Segments = []state.InventorySegment{segment}
				manifest.State = "partial"
			},
			match: "neither uniquely present nor missing",
		},
		{
			name: "missing stream carries fields",
			mutate: func(manifest *state.RouteManifest) {
				manifest.State = "partial"
				manifest.Segments[0].Streams[0].Status = "missing"
			},
			match: "contains file fields",
		},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			manifest := validManifest()
			test.mutate(&manifest)
			if err := ValidateAndNormalize(&manifest); err == nil ||
				!strings.Contains(err.Error(), test.match) {
				t.Fatalf("expected error containing %q, got %v", test.match, err)
			}
		})
	}
}

func validManifest() state.RouteManifest {
	file := state.InventoryFile{
		ArtifactType: "rlog",
		Camera:       nil,
		MTimeNS:      100,
		RelativePath: "realdata/route--0/rlog.zst",
		SHA256:       strings.Repeat("a", 64),
		Size:         10,
	}
	return state.RouteManifest{
		CapabilitySource: "configured+route_union",
		ClosedAt:         time.Date(2026, 7, 29, 12, 0, 0, 0, time.UTC),
		ClosureEvidence: []string{
			"offroad",
			"no_lock",
			"stable_duration",
		},
		ExpectedStreams: []state.ExpectedStream{{
			ArtifactType: "rlog",
			Camera:       nil,
			Role:         "realdata|rlog|-",
			RootName:     "realdata",
		}},
		Generation:            1,
		MissingSegmentNumbers: []int{},
		RootNames:             []string{"realdata"},
		RouteClosed:           true,
		RouteFiles:            []state.InventoryFile{},
		RouteName:             "route",
		Schema:                Schema,
		SchemaVersion:         SchemaVersion,
		Segments: []state.InventorySegment{{
			Files:   []state.InventoryFile{file},
			Number:  0,
			Streams: []state.InventoryStream{presentStream("realdata|rlog|-", file)},
		}},
		State: "complete",
	}
}

func presentStream(role string, file state.InventoryFile) state.InventoryStream {
	relativePath := file.RelativePath
	mtime := file.MTimeNS
	digest := file.SHA256
	size := file.Size
	return state.InventoryStream{
		MTimeNS:      &mtime,
		RelativePath: &relativePath,
		Role:         role,
		SHA256:       &digest,
		Size:         &size,
		Status:       "present",
	}
}

func stringTestPointer(value string) *string {
	return &value
}

func reverseExpected(values []state.ExpectedStream) {
	for left, right := 0, len(values)-1; left < right; left, right = left+1, right-1 {
		values[left], values[right] = values[right], values[left]
	}
}

func reverseFiles(values []state.InventoryFile) {
	for left, right := 0, len(values)-1; left < right; left, right = left+1, right-1 {
		values[left], values[right] = values[right], values[left]
	}
}

func reverseStreams(values []state.InventoryStream) {
	for left, right := 0, len(values)-1; left < right; left, right = left+1, right-1 {
		values[left], values[right] = values[right], values[left]
	}
}

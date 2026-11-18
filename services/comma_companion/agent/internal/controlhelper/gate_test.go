package controlhelper

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestIndependentOffroadGateRequiresAdvancingRawPandaIgnitionOff(t *testing.T) {
	now := time.Date(2026, 7, 29, 12, 0, 0, 0, time.UTC)
	dir := t.TempDir()
	offroad := filepath.Join(dir, "IsOffroad")
	onroad := filepath.Join(dir, "IsOnroad")
	proofPath := filepath.Join(dir, "panda-ignition.json")
	writeGateState(t, offroad, "1", now.Add(-time.Minute))
	writeGateState(t, onroad, "0", now.Add(-time.Minute))
	writePandaProof(t, proofPath, validPandaProof(now.Add(-time.Second), 41))
	immediate := make(chan time.Time)
	close(immediate)
	gate := OffroadGate{
		OffroadPath:     offroad,
		OnroadPath:      onroad,
		PandaProofPath:  proofPath,
		StableFor:       10 * time.Second,
		MaximumAge:      time.Hour,
		PandaMaximumAge: 2 * time.Second,
		Now:             func() time.Time { return now },
		After: func(time.Duration) <-chan time.Time {
			writePandaProof(t, proofPath, validPandaProof(now.Add(-500*time.Millisecond), 42))
			return immediate
		},
		readPandaProof: testPandaProofReader,
	}
	if err := gate.Check(context.Background()); err != nil {
		t.Fatal(err)
	}
}

func TestIndependentOffroadGateFailsClosed(t *testing.T) {
	now := time.Date(2026, 7, 29, 12, 0, 0, 0, time.UTC)
	tests := []struct {
		name        string
		offroad     string
		onroad      string
		modified    time.Time
		firstProof  *PandaIgnitionProof
		secondProof *PandaIgnitionProof
		match       string
	}{
		{
			name:     "onroad",
			offroad:  "0",
			onroad:   "1",
			modified: now,
			match:    "not consistently offroad",
		},
		{
			name:     "stale offroad params",
			offroad:  "1",
			onroad:   "0",
			modified: now.Add(-2 * time.Hour),
			match:    "stale",
		},
		{
			name:     "missing panda proof",
			offroad:  "1",
			onroad:   "0",
			modified: now,
			match:    "raw panda ignition proof",
		},
		{
			name:       "stale panda proof",
			offroad:    "1",
			onroad:     "0",
			modified:   now,
			firstProof: pandaProofPointer(validPandaProof(now.Add(-3*time.Second), 1)),
			match:      "stale",
		},
		{
			name:     "unknown panda",
			offroad:  "1",
			onroad:   "0",
			modified: now,
			firstProof: pandaProofPointer(PandaIgnitionProof{
				Version:    pandaIgnitionProofVersion,
				Source:     "pandaStates",
				ObservedAt: now,
				Sequence:   1,
				Pandas: []PandaIgnitionSample{{
					Index:     0,
					PandaType: "unknown",
				}},
			}),
			match: "unknown panda_type",
		},
		{
			name:     "ignition line on",
			offroad:  "1",
			onroad:   "0",
			modified: now,
			firstProof: func() *PandaIgnitionProof {
				proof := validPandaProof(now, 1)
				proof.Pandas[0].IgnitionLine = true
				return &proof
			}(),
			match: "ignition is on",
		},
		{
			name:     "ignition CAN on",
			offroad:  "1",
			onroad:   "0",
			modified: now,
			firstProof: func() *PandaIgnitionProof {
				proof := validPandaProof(now, 1)
				proof.Pandas[0].IgnitionCAN = true
				return &proof
			}(),
			match: "ignition is on",
		},
		{
			name:        "proof does not advance",
			offroad:     "1",
			onroad:      "0",
			modified:    now,
			firstProof:  pandaProofPointer(validPandaProof(now.Add(-time.Second), 4)),
			secondProof: pandaProofPointer(validPandaProof(now.Add(-time.Second), 4)),
			match:       "did not advance",
		},
		{
			name:       "panda set changed",
			offroad:    "1",
			onroad:     "0",
			modified:   now,
			firstProof: pandaProofPointer(validPandaProof(now.Add(-time.Second), 4)),
			secondProof: func() *PandaIgnitionProof {
				proof := validPandaProof(now.Add(-500*time.Millisecond), 5)
				proof.Pandas[0].PandaType = "tres"
				return &proof
			}(),
			match: "panda set or ignition state changed",
		},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			dir := t.TempDir()
			offroad := filepath.Join(dir, "IsOffroad")
			onroad := filepath.Join(dir, "IsOnroad")
			proofPath := filepath.Join(dir, "panda-ignition.json")
			writeGateState(t, offroad, test.offroad, test.modified)
			writeGateState(t, onroad, test.onroad, test.modified)
			if test.firstProof != nil {
				writePandaProof(t, proofPath, *test.firstProof)
			}
			immediate := make(chan time.Time)
			close(immediate)
			gate := OffroadGate{
				OffroadPath:     offroad,
				OnroadPath:      onroad,
				PandaProofPath:  proofPath,
				StableFor:       time.Second,
				MaximumAge:      time.Hour,
				PandaMaximumAge: 2 * time.Second,
				Now:             func() time.Time { return now },
				After: func(time.Duration) <-chan time.Time {
					if test.secondProof != nil {
						writePandaProof(t, proofPath, *test.secondProof)
					}
					return immediate
				},
				readPandaProof: testPandaProofReader,
			}
			if err := gate.Check(context.Background()); err == nil ||
				!strings.Contains(err.Error(), test.match) {
				t.Fatalf("expected %q failure, got %v", test.match, err)
			}
		})
	}
}

func TestPandaProofPathRejectsSymlinkAndUnknownFields(t *testing.T) {
	now := time.Date(2026, 7, 29, 12, 0, 0, 0, time.UTC)
	dir := t.TempDir()
	target := filepath.Join(dir, "proof.json")
	writePandaProof(t, target, validPandaProof(now, 1))
	link := filepath.Join(dir, "proof-link.json")
	if err := os.Symlink(target, link); err == nil {
		if _, err := testPandaProofReader(link); err == nil ||
			!strings.Contains(err.Error(), "non-symlink") {
			t.Fatalf("symlink proof was not rejected: %v", err)
		}
	}
	unknown := filepath.Join(dir, "unknown.json")
	raw := []byte(`{"version":1,"source":"pandaStates","observed_at":"2026-07-29T12:00:00Z","sequence":1,"pandas":[{"index":0,"panda_type":"dos","ignition_line":false,"ignition_can":false}],"extra":true}`)
	if err := os.WriteFile(unknown, raw, 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := testPandaProofReader(unknown); err == nil ||
		!strings.Contains(err.Error(), "unknown field") {
		t.Fatalf("unknown proof field was not rejected: %v", err)
	}
}

func validPandaProof(observedAt time.Time, sequence uint64) PandaIgnitionProof {
	return PandaIgnitionProof{
		Version:    pandaIgnitionProofVersion,
		Source:     "pandaStates",
		ObservedAt: observedAt.UTC(),
		Sequence:   sequence,
		Pandas: []PandaIgnitionSample{{
			Index:     0,
			PandaType: "dos",
		}},
	}
}

func pandaProofPointer(proof PandaIgnitionProof) *PandaIgnitionProof {
	return &proof
}

func writePandaProof(t *testing.T, path string, proof PandaIgnitionProof) {
	t.Helper()
	raw, err := json.Marshal(proof)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, raw, 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.Chtimes(path, proof.ObservedAt, proof.ObservedAt); err != nil {
		t.Fatal(err)
	}
}

func testPandaProofReader(path string) (pandaProofSnapshot, error) {
	return readPandaProof(path, func(os.FileInfo) error {
		return nil
	})
}

func writeGateState(t *testing.T, path, value string, modified time.Time) {
	t.Helper()
	if err := os.WriteFile(path, []byte(value), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.Chtimes(path, modified, modified); err != nil {
		t.Fatal(err)
	}
}

func TestPandaProofReaderRequiresOwnerValidator(t *testing.T) {
	path := filepath.Join(t.TempDir(), "proof.json")
	writePandaProof(t, path, validPandaProof(time.Now().UTC(), 1))
	if _, err := readPandaProof(path, nil); err == nil ||
		!strings.Contains(err.Error(), "owner validator") {
		t.Fatalf("missing owner validator was not rejected: %v", err)
	}
}

package journal

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"

	"starpilot.local/comma-companion-agent/internal/state"
)

func TestJournalCodecCoversEveryField(t *testing.T) {
	data := state.EmptyJournal()
	fields := journalFields(&data)
	typ := reflect.TypeOf(data)
	if len(fields) != typ.NumField() {
		t.Fatal("journal fields changed without updating streaming codec")
	}
	for i := range typ.NumField() {
		field := typ.Field(i)
		name := strings.Split(field.Tag.Get("json"), ",")[0]
		found := false
		for _, encoded := range fields {
			if encoded.name == name && reflect.ValueOf(encoded.value).Pointer() == reflect.ValueOf(&data).Elem().Field(i).Addr().Pointer() {
				found = true
			}
		}
		if !found {
			t.Errorf("missing journal field: %s", field.Name)
		}
	}
}

func TestStreamingEncodeFailurePreservesJournalAndWAL(t *testing.T) {
	path := filepath.Join(t.TempDir(), "journal.json")
	store, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	if err := store.Update(func(d *state.Journal) error { d.Files["file"] = state.File{ID: "file"}; return nil }); err != nil {
		t.Fatal(err)
	}
	if err := store.UpdateFile("file", func(f *state.File, _ *state.Counters) (bool, error) { f.UploadOffset = 123; return true, nil }); err != nil {
		t.Fatal(err)
	}
	base, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	wal, err := os.ReadFile(path + ".wal")
	if err != nil {
		t.Fatal(err)
	}
	err = store.Update(func(d *state.Journal) error {
		d.LastScanAt = time.Date(10000, 1, 1, 0, 0, 0, 0, time.UTC)
		return nil
	})
	if err == nil {
		t.Fatal("expected time encoding failure after partial output")
	}
	for name, want := range map[string][]byte{path: base, path + ".wal": wal} {
		got, err := os.ReadFile(name)
		if err != nil || !bytes.Equal(want, got) {
			t.Fatalf("failed update altered %s: %v", name, err)
		}
	}
	if !store.View().LastScanAt.IsZero() {
		t.Fatal("failed generation became visible")
	}
	reopened, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	if reopened.View().Files["file"].UploadOffset != 123 {
		t.Fatal("WAL update lost")
	}
	temps, err := filepath.Glob(filepath.Join(filepath.Dir(path), ".journal-*.tmp"))
	if err != nil || len(temps) != 0 {
		t.Fatalf("temporary output leaked: %v %v", temps, err)
	}
}

func TestJournalCodecCompatibility(t *testing.T) {
	for _, input := range []string{
		`null`, `{}`, `{"VERSION":4,"paused":true,"mutation_sequence":42}`,
		`{"files":null,"observations":{},"commands":null,"route_inventories":{}}`,
		`{"unknown":{"nested":[true,null,{"large":1e1000}]},"version":4}`,
		`{"files":{"a":{"id":"first"}},"files":{"b":{"id":"second"}}}`,
		`{"version":4,"mutation_sequence":42,"paused":true,"last_scan_at":"2026-09-12T12:00:00Z",
		"observations":{"a":{"source_path":"a","stable_count":2}},
		"files":{"quote\"\\key":{"id":"id","state":"uploading","upload_offset":9,"completion_evidence":["closed"]}},
		"upload_cancellations":{"cancel":{"file_id":"id","upload_id":"upload","attempts":3}},
		"commands":{"command":{"id":"command","reported":true}},"counters":{"scan_errors":5},
		"route_inventories":{"manifest":{"declared_at":"2026-09-12T12:00:00Z","manifest_sha256":"hash","manifest":{
		"route_name":"route","previous_manifest_sha256":"previous","root_names":["realdata"],
		"route_files":[{"artifact_type":"qlog","camera":null}],
		"expected_streams":[{"camera":"road"}],"segments":[{"number":3,
		"files":[{"relative_path":"a","sha256":"abc","size":12}],
		"streams":[{"relative_path":"a","size":12,"mtime_ns":123,"sha256":"abc"}]}]}}}}`,
	} {
		t.Run(input, func(t *testing.T) {
			want := state.EmptyJournal()
			if err := json.Unmarshal([]byte(input), &want); err != nil {
				t.Fatal(err)
			}
			got := state.EmptyJournal()
			if err := decodeJournal(strings.NewReader(input), &got); err != nil {
				t.Fatal(err)
			}
			if !reflect.DeepEqual(want, got) {
				t.Fatal("streaming decode differs from legacy reader")
			}
			var encoded bytes.Buffer
			if err := encodeJournal(&encoded, &got); err != nil {
				t.Fatal(err)
			}
			var legacy state.Journal
			if err := json.Unmarshal(encoded.Bytes(), &legacy); err != nil {
				t.Fatal(err)
			}
			if !reflect.DeepEqual(got, legacy) {
				t.Fatal("legacy reader cannot recover streamed journal")
			}
			var again bytes.Buffer
			if err := encodeJournal(&again, &got); err != nil {
				t.Fatal(err)
			}
			if !bytes.Equal(encoded.Bytes(), again.Bytes()) {
				t.Fatal("encoding is not deterministic")
			}
		})
	}
}

func TestJournalCodecRejectsCorruption(t *testing.T) {
	for _, input := range []string{
		``, `[1]`, `{"version":`, `{"version":4} {}`, `{"version":4} garbage`,
		`{"files":[]}`, `{"files":{"a":`, `{"files":{"a":42}}`,
		`{"unknown":{"array":[1,2}`, `{"paused":"yes"}`,
	} {
		data := state.EmptyJournal()
		if err := decodeJournal(strings.NewReader(input), &data); err == nil {
			t.Errorf("accepted invalid journal: %s", input)
		}
	}
}

type failingJournalWriter struct{}

func (failingJournalWriter) Write([]byte) (int, error) { return 0, io.ErrClosedPipe }

func TestJournalCodecPropagatesWriteFailure(t *testing.T) {
	data := state.EmptyJournal()
	if err := encodeJournal(failingJournalWriter{}, &data); !errors.Is(err, io.ErrClosedPipe) {
		t.Fatalf("write failure lost: %v", err)
	}
}

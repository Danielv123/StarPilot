package journal

import (
	"bufio"
	"encoding/json"
	"fmt"
	"io"
	"sort"
	"strings"

	"starpilot.local/comma-companion-agent/internal/state"
)

type journalField struct {
	name  string
	value any
}

func journalFields(data *state.Journal) []journalField {
	return []journalField{
		{"version", &data.Version},
		{"mutation_sequence", &data.MutationSequence},
		{"paused", &data.Paused},
		{"observations", &data.Observations},
		{"files", &data.Files},
		{"upload_cancellations", &data.Cancellations},
		{"route_inventories", &data.Inventories},
		{"commands", &data.Commands},
		{"counters", &data.Counters},
		{"last_scan_at", &data.LastScanAt},
	}
}

// Decode one map entry at a time. Decoder.Decode(&journal) buffers the entire
// document, just as ReadFile/Unmarshal does. Historical route inventories can
// dwarf the active queue, so that extra full-size buffer is significant.
func decodeJournal(r io.Reader, data *state.Journal) error {
	dec := json.NewDecoder(r)
	dec.UseNumber()
	fields := make(map[string]any)
	for _, field := range journalFields(data) {
		fields[field.name] = field.value
	}
	if err := decodeObject(dec, func(key string) error {
		value, ok := fields[key]
		if !ok {
			for name, candidate := range fields {
				if strings.EqualFold(name, key) {
					value, ok = candidate, true
					break
				}
			}
		}
		if !ok {
			// Preserve encoding/json's acceptance of unknown fields.
			return skipValue(dec)
		}
		switch target := value.(type) {
		case *map[string]state.Observation:
			return decodeMap(dec, target)
		case *map[string]state.File:
			return decodeMap(dec, target)
		case *map[string]state.UploadCancellation:
			return decodeMap(dec, target)
		case *map[string]state.RouteInventory:
			return decodeMap(dec, target)
		case *map[string]state.CommandRecord:
			return decodeMap(dec, target)
		default:
			return dec.Decode(value)
		}
	}); err != nil {
		return err
	}
	if _, err := dec.Token(); err != io.EOF {
		if err == nil {
			return fmt.Errorf("unexpected data after journal")
		}
		return err
	}
	return nil
}

func decodeObject(dec *json.Decoder, entry func(string) error) error {
	token, err := dec.Token()
	if err != nil {
		return err
	}
	if token == nil {
		return nil
	}
	if token != json.Delim('{') {
		return fmt.Errorf("expected JSON object, got %v", token)
	}
	for dec.More() {
		key, err := dec.Token()
		if err != nil {
			return err
		}
		name, ok := key.(string)
		if !ok {
			return fmt.Errorf("expected object key")
		}
		if err := entry(name); err != nil {
			return fmt.Errorf("%s: %w", name, err)
		}
	}
	_, err = dec.Token()
	return err
}

func decodeMap[T any](dec *json.Decoder, target *map[string]T) error {
	// Peek via Token so even null/empty maps retain standard JSON semantics.
	token, err := dec.Token()
	if err != nil {
		return err
	}
	if token == nil {
		*target = nil
		return nil
	}
	if token != json.Delim('{') {
		return fmt.Errorf("expected map object")
	}
	if *target == nil {
		*target = make(map[string]T)
	}
	for dec.More() {
		key, err := dec.Token()
		if err != nil {
			return err
		}
		name, ok := key.(string)
		if !ok {
			return fmt.Errorf("expected map key")
		}
		var value T
		if err := dec.Decode(&value); err != nil {
			return err
		}
		(*target)[name] = value
	}
	_, err = dec.Token()
	return err
}

func skipValue(dec *json.Decoder) error {
	token, err := dec.Token()
	if err != nil {
		return err
	}
	if delim, ok := token.(json.Delim); ok && (delim == '{' || delim == '[') {
		for dec.More() {
			if delim == '{' {
				if _, err := dec.Token(); err != nil {
					return err
				}
			}
			if err := skipValue(dec); err != nil {
				return err
			}
		}
		_, err = dec.Token()
	}
	return err
}

// Keep the existing journal schema and atomic replacement, but never marshal
// the whole archive index into one temporary byte slice. A single encoder
// reuses a buffer bounded by the largest individual record.
func encodeJournal(w io.Writer, data *state.Journal) error {
	buf := bufio.NewWriter(w)
	enc := json.NewEncoder(buf)
	if _, err := buf.WriteString("{"); err != nil {
		return err
	}
	first := true
	for _, field := range journalFields(data) {
		if field.name == "mutation_sequence" && data.MutationSequence == 0 {
			continue
		}
		if !first {
			if _, err := buf.WriteString(","); err != nil {
				return err
			}
		}
		first = false
		if err := writeKey(buf, field.name); err != nil {
			return err
		}
		var err error
		switch target := field.value.(type) {
		case *map[string]state.Observation:
			err = encodeMap(buf, enc, *target)
		case *map[string]state.File:
			err = encodeMap(buf, enc, *target)
		case *map[string]state.UploadCancellation:
			err = encodeMap(buf, enc, *target)
		case *map[string]state.RouteInventory:
			err = encodeMap(buf, enc, *target)
		case *map[string]state.CommandRecord:
			err = encodeMap(buf, enc, *target)
		default:
			err = enc.Encode(field.value)
		}
		if err != nil {
			return err
		}
	}
	if _, err := buf.WriteString("}"); err != nil {
		return err
	}
	return buf.Flush()
}

func writeKey(w *bufio.Writer, key string) error {
	encoded, err := json.Marshal(key)
	if err != nil {
		return err
	}
	if _, err := w.Write(encoded); err != nil {
		return err
	}
	return w.WriteByte(':')
}

func encodeMap[T any](w *bufio.Writer, enc *json.Encoder, values map[string]T) error {
	if values == nil {
		_, err := w.WriteString("null")
		return err
	}
	if err := w.WriteByte('{'); err != nil {
		return err
	}
	keys := make([]string, 0, len(values))
	for key := range values {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	for i, key := range keys {
		if i > 0 {
			if err := w.WriteByte(','); err != nil {
				return err
			}
		}
		if err := writeKey(w, key); err != nil {
			return err
		}
		if err := enc.Encode(values[key]); err != nil {
			return err
		}
	}
	return w.WriteByte('}')
}

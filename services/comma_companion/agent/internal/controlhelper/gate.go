package controlhelper

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"sort"
	"strings"
	"time"
)

const pandaIgnitionProofVersion = 1

type OffroadGate struct {
	OffroadPath     string
	OnroadPath      string
	PandaProofPath  string
	StableFor       time.Duration
	MaximumAge      time.Duration
	PandaMaximumAge time.Duration
	Now             func() time.Time
	After           func(time.Duration) <-chan time.Time
	readPandaProof  func(string) (pandaProofSnapshot, error)
}

type boolSnapshot struct {
	value   bool
	info    os.FileInfo
	modTime time.Time
	size    int64
}

type PandaIgnitionProof struct {
	Version    int                   `json:"version"`
	Source     string                `json:"source"`
	ObservedAt time.Time             `json:"observed_at"`
	Sequence   uint64                `json:"sequence"`
	Pandas     []PandaIgnitionSample `json:"pandas"`
}

type PandaIgnitionSample struct {
	Index        int    `json:"index"`
	PandaType    string `json:"panda_type"`
	IgnitionLine bool   `json:"ignition_line"`
	IgnitionCAN  bool   `json:"ignition_can"`
}

type pandaProofSnapshot struct {
	proof   PandaIgnitionProof
	info    os.FileInfo
	modTime time.Time
	size    int64
}

func (g OffroadGate) Check(ctx context.Context) error {
	if g.StableFor <= 0 || g.MaximumAge <= 0 || g.PandaMaximumAge <= 0 {
		return errors.New("offroad gate durations must be positive")
	}
	if strings.TrimSpace(g.PandaProofPath) == "" {
		return errors.New("raw panda ignition proof path is required")
	}
	if g.Now == nil {
		g.Now = time.Now
	}
	if g.After == nil {
		g.After = time.After
	}
	if g.readPandaProof == nil {
		g.readPandaProof = securePandaProof
	}
	firstOffroad, err := secureBool(g.OffroadPath)
	if err != nil {
		return fmt.Errorf("read IsOffroad: %w", err)
	}
	firstOnroad, err := secureBool(g.OnroadPath)
	if err != nil {
		return fmt.Errorf("read IsOnroad: %w", err)
	}
	if !firstOffroad.value || firstOnroad.value {
		return errors.New("device is not consistently offroad")
	}
	if err := g.requireFresh(firstOffroad, firstOnroad); err != nil {
		return err
	}
	firstPanda, err := g.readPandaProof(g.PandaProofPath)
	if err != nil {
		return fmt.Errorf("read raw panda ignition proof: %w", err)
	}
	if err := g.requirePandaIgnitionOff(firstPanda); err != nil {
		return err
	}
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-g.After(g.StableFor):
	}
	secondOffroad, err := secureBool(g.OffroadPath)
	if err != nil {
		return fmt.Errorf("reread IsOffroad: %w", err)
	}
	secondOnroad, err := secureBool(g.OnroadPath)
	if err != nil {
		return fmt.Errorf("reread IsOnroad: %w", err)
	}
	if !secondOffroad.value || secondOnroad.value ||
		!sameBoolSnapshot(firstOffroad, secondOffroad) ||
		!sameBoolSnapshot(firstOnroad, secondOnroad) {
		return errors.New("offroad state changed during the independent stability gate")
	}
	if err := g.requireFresh(secondOffroad, secondOnroad); err != nil {
		return err
	}
	secondPanda, err := g.readPandaProof(g.PandaProofPath)
	if err != nil {
		return fmt.Errorf("reread raw panda ignition proof: %w", err)
	}
	if err := g.requirePandaIgnitionOff(secondPanda); err != nil {
		return err
	}
	if !samePandaSetAndIgnition(firstPanda.proof, secondPanda.proof) {
		return errors.New("raw panda set or ignition state changed during the stability gate")
	}
	if secondPanda.proof.Sequence <= firstPanda.proof.Sequence ||
		!secondPanda.proof.ObservedAt.After(firstPanda.proof.ObservedAt) {
		return errors.New("raw panda ignition proof did not advance during the stability gate")
	}
	return nil
}

func (g OffroadGate) requireFresh(values ...boolSnapshot) error {
	now := g.Now().UTC()
	for _, value := range values {
		age := now.Sub(value.modTime)
		if age < -time.Minute || age > g.MaximumAge {
			return errors.New("offroad state source is stale or materially future-dated")
		}
	}
	return nil
}

func (g OffroadGate) requirePandaIgnitionOff(snapshot pandaProofSnapshot) error {
	now := g.Now().UTC()
	observedAge := now.Sub(snapshot.proof.ObservedAt.UTC())
	fileAge := now.Sub(snapshot.modTime.UTC())
	if observedAge < -time.Second || observedAge > g.PandaMaximumAge ||
		fileAge < -time.Second || fileAge > g.PandaMaximumAge {
		return errors.New("raw panda ignition proof is stale or future-dated")
	}
	for _, panda := range snapshot.proof.Pandas {
		if panda.IgnitionLine || panda.IgnitionCAN {
			return errors.New("raw panda ignition is on")
		}
	}
	return nil
}

func secureBool(path string) (boolSnapshot, error) {
	pathInfo, err := os.Lstat(path)
	if err != nil {
		return boolSnapshot{}, err
	}
	if pathInfo.Mode()&os.ModeSymlink != 0 || !pathInfo.Mode().IsRegular() {
		return boolSnapshot{}, errors.New("state path is not a regular non-symlink file")
	}
	handle, err := os.Open(path)
	if err != nil {
		return boolSnapshot{}, err
	}
	defer handle.Close()
	info, err := handle.Stat()
	if err != nil || !os.SameFile(pathInfo, info) {
		return boolSnapshot{}, errors.New("state file changed identity while opening")
	}
	raw, err := io.ReadAll(io.LimitReader(handle, 17))
	if err != nil || len(raw) > 16 {
		return boolSnapshot{}, errors.New("state file is unreadable or oversized")
	}
	finalInfo, err := handle.Stat()
	if err != nil || !os.SameFile(info, finalInfo) ||
		info.Size() != finalInfo.Size() ||
		info.ModTime() != finalInfo.ModTime() {
		return boolSnapshot{}, errors.New("state file changed while reading")
	}
	currentPath, err := os.Lstat(path)
	if err != nil || !os.SameFile(finalInfo, currentPath) {
		return boolSnapshot{}, errors.New("state path changed after reading")
	}
	text := strings.ToLower(strings.TrimSpace(string(raw)))
	var value bool
	switch text {
	case "1", "true":
		value = true
	case "0", "false":
		value = false
	default:
		return boolSnapshot{}, errors.New("state file does not contain a canonical boolean")
	}
	return boolSnapshot{
		value:   value,
		info:    finalInfo,
		modTime: finalInfo.ModTime(),
		size:    finalInfo.Size(),
	}, nil
}

func securePandaProof(path string) (pandaProofSnapshot, error) {
	return readPandaProof(path, validateRootOwnedProof)
}

func readPandaProof(
	path string,
	validateOwner func(os.FileInfo) error,
) (pandaProofSnapshot, error) {
	pathInfo, err := os.Lstat(path)
	if err != nil {
		return pandaProofSnapshot{}, err
	}
	if pathInfo.Mode()&os.ModeSymlink != 0 || !pathInfo.Mode().IsRegular() {
		return pandaProofSnapshot{}, errors.New("proof path is not a regular non-symlink file")
	}
	if validateOwner == nil {
		return pandaProofSnapshot{}, errors.New("proof owner validator is required")
	}
	if err := validateOwner(pathInfo); err != nil {
		return pandaProofSnapshot{}, err
	}
	handle, err := os.Open(path)
	if err != nil {
		return pandaProofSnapshot{}, err
	}
	defer handle.Close()
	info, err := handle.Stat()
	if err != nil || !os.SameFile(pathInfo, info) {
		return pandaProofSnapshot{}, errors.New("proof file changed identity while opening")
	}
	raw, err := io.ReadAll(io.LimitReader(handle, 64<<10+1))
	if err != nil || len(raw) == 0 || len(raw) > 64<<10 {
		return pandaProofSnapshot{}, errors.New("proof file is empty, unreadable, or oversized")
	}
	finalInfo, err := handle.Stat()
	if err != nil || !os.SameFile(info, finalInfo) ||
		info.Size() != finalInfo.Size() ||
		info.ModTime() != finalInfo.ModTime() {
		return pandaProofSnapshot{}, errors.New("proof file changed while reading")
	}
	currentPath, err := os.Lstat(path)
	if err != nil || !os.SameFile(finalInfo, currentPath) {
		return pandaProofSnapshot{}, errors.New("proof path changed after reading")
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	var proof PandaIgnitionProof
	if err := decoder.Decode(&proof); err != nil {
		return pandaProofSnapshot{}, fmt.Errorf("decode proof: %w", err)
	}
	var trailing any
	if err := decoder.Decode(&trailing); !errors.Is(err, io.EOF) {
		return pandaProofSnapshot{}, errors.New("proof contains trailing JSON")
	}
	if err := validatePandaProof(proof); err != nil {
		return pandaProofSnapshot{}, err
	}
	return pandaProofSnapshot{
		proof:   proof,
		info:    finalInfo,
		modTime: finalInfo.ModTime(),
		size:    finalInfo.Size(),
	}, nil
}

func validatePandaProof(proof PandaIgnitionProof) error {
	if proof.Version != pandaIgnitionProofVersion || proof.Source != "pandaStates" {
		return errors.New("proof version or source is unsupported")
	}
	if proof.ObservedAt.IsZero() || proof.ObservedAt.Location() != time.UTC {
		return errors.New("proof observed_at must be a non-zero UTC timestamp")
	}
	if proof.Sequence == 0 {
		return errors.New("proof sequence must be positive")
	}
	if len(proof.Pandas) == 0 || len(proof.Pandas) > 8 {
		return errors.New("proof must contain between one and eight known pandas")
	}
	knownTypes := map[string]bool{
		"whitePanda": true,
		"greyPanda":  true,
		"blackPanda": true,
		"pedal":      true,
		"uno":        true,
		"dos":        true,
		"redPanda":   true,
		"redPandaV2": true,
		"tres":       true,
		"cuatro":     true,
	}
	for index, panda := range proof.Pandas {
		if panda.Index != index {
			return errors.New("proof panda indexes must be contiguous and ordered")
		}
		if !knownTypes[panda.PandaType] {
			return fmt.Errorf("proof panda %d has an unknown panda_type", index)
		}
	}
	return nil
}

func samePandaSetAndIgnition(left, right PandaIgnitionProof) bool {
	if len(left.Pandas) != len(right.Pandas) {
		return false
	}
	leftSamples := append([]PandaIgnitionSample(nil), left.Pandas...)
	rightSamples := append([]PandaIgnitionSample(nil), right.Pandas...)
	sort.Slice(leftSamples, func(i, j int) bool {
		return leftSamples[i].Index < leftSamples[j].Index
	})
	sort.Slice(rightSamples, func(i, j int) bool {
		return rightSamples[i].Index < rightSamples[j].Index
	})
	for index := range leftSamples {
		if leftSamples[index] != rightSamples[index] {
			return false
		}
	}
	return true
}

func sameBoolSnapshot(left, right boolSnapshot) bool {
	return left.value == right.value &&
		left.size == right.size &&
		left.modTime == right.modTime &&
		os.SameFile(left.info, right.info)
}

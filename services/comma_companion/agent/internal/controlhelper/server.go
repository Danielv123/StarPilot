package controlhelper

import (
	"bufio"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"path/filepath"
	"sort"
	"sync"
	"time"

	"starpilot.local/comma-companion-agent/internal/control"
)

const ledgerVersion = 1

type PeerValidator func(net.Conn) error
type Gate func(context.Context) error
type Executor func(context.Context, control.Operation, string) error

type Server struct {
	Secret       []byte
	ClockSkew    time.Duration
	ValidatePeer PeerValidator
	CheckOffroad Gate
	Execute      Executor
	Ledger       *Ledger
	Now          func() time.Time
	actionMu     sync.Mutex
}

type ledgerRecord struct {
	Fingerprint string    `json:"fingerprint"`
	State       string    `json:"state"`
	Error       string    `json:"error,omitempty"`
	FinishedAt  time.Time `json:"finished_at,omitempty"`
}

type ledgerData struct {
	Version int                     `json:"version"`
	Records map[string]ledgerRecord `json:"records"`
}

type Ledger struct {
	path string
	mu   sync.Mutex
	data ledgerData
}

func OpenLedger(path string) (*Ledger, error) {
	result := &Ledger{
		path: path,
		data: ledgerData{
			Version: ledgerVersion,
			Records: make(map[string]ledgerRecord),
		},
	}
	raw, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		if err := result.persistLocked(); err != nil {
			return nil, err
		}
		return result, nil
	}
	if err != nil {
		return nil, fmt.Errorf("read control ledger: %w", err)
	}
	if err := json.Unmarshal(raw, &result.data); err != nil {
		return nil, fmt.Errorf("parse control ledger: %w", err)
	}
	if result.data.Version != ledgerVersion || result.data.Records == nil {
		return nil, errors.New("unsupported or incomplete control ledger")
	}
	return result, nil
}

func (s *Server) Serve(ctx context.Context, listener net.Listener) error {
	if len(s.Secret) < 16 || s.ValidatePeer == nil || s.CheckOffroad == nil ||
		s.Execute == nil || s.Ledger == nil {
		return errors.New("privileged control server is incompletely configured")
	}
	if s.Now == nil {
		s.Now = time.Now
	}
	go func() {
		<-ctx.Done()
		_ = listener.Close()
	}()
	for {
		connection, err := listener.Accept()
		if err != nil {
			if ctx.Err() != nil {
				return ctx.Err()
			}
			return fmt.Errorf("accept privileged control connection: %w", err)
		}
		go s.handle(ctx, connection)
	}
}

func (s *Server) handle(ctx context.Context, connection net.Conn) {
	defer connection.Close()
	_ = connection.SetDeadline(time.Now().Add(45 * time.Second))
	response := control.Response{State: "failed"}
	if err := s.ValidatePeer(connection); err != nil {
		response.Error = "unauthorized local control peer"
		_ = json.NewEncoder(connection).Encode(response)
		return
	}
	reader := bufio.NewReaderSize(connection, 64<<10)
	line, err := reader.ReadBytes('\n')
	if err != nil || len(line) > 64<<10 {
		response.Error = "invalid privileged control request framing"
		_ = json.NewEncoder(connection).Encode(response)
		return
	}
	decoder := json.NewDecoder(bytes.NewReader(line))
	decoder.DisallowUnknownFields()
	var request control.Request
	if err := decoder.Decode(&request); err != nil {
		response.Error = "invalid privileged control request"
		_ = json.NewEncoder(connection).Encode(response)
		return
	}
	response.RequestID = request.RequestID
	var trailing any
	if err := decoder.Decode(&trailing); !errors.Is(err, io.EOF) {
		response.Error = "invalid privileged control request framing"
		_ = json.NewEncoder(connection).Encode(response)
		return
	}
	now := s.Now().UTC()
	if err := control.ValidateRequest(request, now, s.ClockSkew); err != nil ||
		!control.Authenticate(s.Secret, request) {
		response.Error = "invalid or unauthenticated privileged control request"
		_ = json.NewEncoder(connection).Encode(response)
		return
	}
	response = s.executeAuthenticated(ctx, request, now)
	_ = json.NewEncoder(connection).Encode(response)
}

func (s *Server) executeAuthenticated(
	ctx context.Context,
	request control.Request,
	now time.Time,
) control.Response {
	s.actionMu.Lock()
	defer s.actionMu.Unlock()
	response := control.Response{RequestID: request.RequestID}
	fingerprint := requestFingerprint(request)
	existing, found := s.Ledger.lookup(request.RequestID)
	if found {
		if existing.Fingerprint != fingerprint {
			response.State = "failed"
			response.Error = "control request_id was reused with different immutable fields"
			return response
		}
		if existing.State == "succeeded" {
			response.State = "succeeded"
			response.Message = "privileged action was already completed"
			return response
		}
		if existing.State == "failed" {
			response.State = "failed"
			response.Error = existing.Error
			return response
		}
	}
	if err := s.CheckOffroad(ctx); err != nil {
		response.State = "failed"
		response.Error = "independent offroad gate rejected action: " + err.Error()
		_ = s.Ledger.finish(request.RequestID, fingerprint, "failed", response.Error, now)
		return response
	}
	if err := s.Ledger.begin(request.RequestID, fingerprint); err != nil {
		response.State = "failed"
		response.Error = err.Error()
		return response
	}
	if err := s.Execute(ctx, request.Operation, request.Reason); err != nil {
		response.State = "failed"
		response.Error = "privileged action failed: " + err.Error()
		_ = s.Ledger.finish(
			request.RequestID,
			fingerprint,
			"failed",
			response.Error,
			s.Now().UTC(),
		)
		return response
	}
	if err := s.Ledger.finish(
		request.RequestID,
		fingerprint,
		"succeeded",
		"",
		s.Now().UTC(),
	); err != nil {
		response.State = "failed"
		response.Error = "persist privileged action success: " + err.Error()
		return response
	}
	response.State = "succeeded"
	response.Message = "privileged action completed"
	return response
}

func (l *Ledger) lookup(requestID string) (ledgerRecord, bool) {
	l.mu.Lock()
	defer l.mu.Unlock()
	record, found := l.data.Records[requestID]
	return record, found
}

func (l *Ledger) begin(requestID, fingerprint string) error {
	l.mu.Lock()
	defer l.mu.Unlock()
	if existing, found := l.data.Records[requestID]; found &&
		existing.Fingerprint != fingerprint {
		return errors.New("control request_id fingerprint mismatch")
	}
	l.data.Records[requestID] = ledgerRecord{
		Fingerprint: fingerprint,
		State:       "running",
	}
	return l.persistLocked()
}

func (l *Ledger) finish(
	requestID, fingerprint, stateValue, message string,
	finishedAt time.Time,
) error {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.data.Records[requestID] = ledgerRecord{
		Fingerprint: fingerprint,
		State:       stateValue,
		Error:       message,
		FinishedAt:  finishedAt,
	}
	pruneLedger(&l.data, 2048)
	return l.persistLocked()
}

func (l *Ledger) persistLocked() error {
	if err := os.MkdirAll(filepath.Dir(l.path), 0o700); err != nil {
		return fmt.Errorf("create control ledger directory: %w", err)
	}
	encoded, err := json.Marshal(l.data)
	if err != nil {
		return fmt.Errorf("encode control ledger: %w", err)
	}
	temporary, err := os.CreateTemp(filepath.Dir(l.path), ".control-ledger-*.tmp")
	if err != nil {
		return fmt.Errorf("create control ledger temporary file: %w", err)
	}
	temporaryPath := temporary.Name()
	defer os.Remove(temporaryPath)
	if err := temporary.Chmod(0o600); err != nil {
		_ = temporary.Close()
		return err
	}
	if _, err := temporary.Write(encoded); err != nil {
		_ = temporary.Close()
		return err
	}
	if err := temporary.Sync(); err != nil {
		_ = temporary.Close()
		return err
	}
	if err := temporary.Close(); err != nil {
		return err
	}
	if err := os.Rename(temporaryPath, l.path); err != nil {
		return err
	}
	if directory, err := os.Open(filepath.Dir(l.path)); err == nil {
		_ = directory.Sync()
		_ = directory.Close()
	}
	return nil
}

func pruneLedger(data *ledgerData, limit int) {
	if len(data.Records) <= limit {
		return
	}
	type candidate struct {
		id       string
		finished time.Time
	}
	candidates := make([]candidate, 0, len(data.Records))
	for id, record := range data.Records {
		if record.State == "succeeded" || record.State == "failed" {
			candidates = append(candidates, candidate{id: id, finished: record.FinishedAt})
		}
	}
	sort.Slice(candidates, func(i, j int) bool {
		return candidates[i].finished.Before(candidates[j].finished)
	})
	for _, candidate := range candidates {
		if len(data.Records) <= limit {
			break
		}
		delete(data.Records, candidate.id)
	}
}

func requestFingerprint(request control.Request) string {
	digest := sha256.Sum256([]byte(fmt.Sprintf(
		"%d\x00%s\x00%s\x00%s\x00%s",
		request.Version,
		request.RequestID,
		request.Operation,
		request.Reason,
		request.Timestamp.UTC().Format(time.RFC3339Nano),
	)))
	return hex.EncodeToString(digest[:])
}

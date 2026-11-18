package controlhelper

import (
	"context"
	"encoding/json"
	"errors"
	"net"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"starpilot.local/comma-companion-agent/internal/control"
)

func TestServerAuthenticatesAndDeduplicatesFixedAction(t *testing.T) {
	secret := []byte("0123456789abcdef0123456789abcdef")
	now := time.Date(2026, 7, 29, 12, 0, 0, 0, time.UTC)
	ledger, err := OpenLedger(filepath.Join(t.TempDir(), "ledger.json"))
	if err != nil {
		t.Fatal(err)
	}
	var mu sync.Mutex
	executions := 0
	gates := 0
	server := &Server{
		Secret:       secret,
		ClockSkew:    time.Minute,
		ValidatePeer: func(net.Conn) error { return nil },
		CheckOffroad: func(context.Context) error {
			mu.Lock()
			defer mu.Unlock()
			gates++
			return nil
		},
		Execute: func(_ context.Context, operation control.Operation, reason string) error {
			if operation != control.RestartStarPilot || reason != "remote StarPilot restart" {
				return errors.New("wrong fixed action")
			}
			mu.Lock()
			defer mu.Unlock()
			executions++
			return nil
		},
		Ledger: ledger,
		Now:    func() time.Time { return now },
	}
	request := signedHelperRequest(secret, now)
	first := sendHelperRequest(t, server, request)
	second := sendHelperRequest(t, server, request)
	if first.State != "succeeded" || second.State != "succeeded" {
		t.Fatalf("valid request failed: first=%#v second=%#v", first, second)
	}
	mu.Lock()
	defer mu.Unlock()
	if executions != 1 || gates != 1 {
		t.Fatalf("deduplication failed: executions=%d gates=%d", executions, gates)
	}
	reopened, err := OpenLedger(filepath.Join(filepath.Dir(ledger.path), "ledger.json"))
	if err != nil {
		t.Fatal(err)
	}
	record, found := reopened.lookup(request.RequestID)
	if !found || record.State != "succeeded" {
		t.Fatalf("durable helper result missing: %#v", record)
	}
}

func TestServerRejectsPeerAuthMutationAndOffroadFailure(t *testing.T) {
	secret := []byte("0123456789abcdef0123456789abcdef")
	now := time.Date(2026, 7, 29, 12, 0, 0, 0, time.UTC)
	tests := []struct {
		name       string
		peer       PeerValidator
		mutate     func(*control.Request)
		gate       Gate
		errorMatch string
	}{
		{
			name: "peer",
			peer: func(net.Conn) error {
				return errors.New("wrong process")
			},
			errorMatch: "unauthorized",
		},
		{
			name: "signature",
			peer: func(net.Conn) error { return nil },
			mutate: func(request *control.Request) {
				request.Reason = "tampered"
			},
			gate:       func(context.Context) error { return nil },
			errorMatch: "unauthenticated",
		},
		{
			name: "offroad",
			peer: func(net.Conn) error { return nil },
			gate: func(context.Context) error {
				return errors.New("onroad")
			},
			errorMatch: "offroad gate",
		},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			ledger, err := OpenLedger(filepath.Join(t.TempDir(), "ledger.json"))
			if err != nil {
				t.Fatal(err)
			}
			executed := false
			gate := test.gate
			if gate == nil {
				gate = func(context.Context) error { return nil }
			}
			server := &Server{
				Secret:       secret,
				ClockSkew:    time.Minute,
				ValidatePeer: test.peer,
				CheckOffroad: gate,
				Execute: func(context.Context, control.Operation, string) error {
					executed = true
					return nil
				},
				Ledger: ledger,
				Now:    func() time.Time { return now },
			}
			request := signedHelperRequest(secret, now)
			if test.mutate != nil {
				test.mutate(&request)
			}
			response := sendHelperRequest(t, server, request)
			if response.State != "failed" ||
				!strings.Contains(response.Error, test.errorMatch) ||
				executed {
				t.Fatalf("unsafe request result: %#v executed=%t", response, executed)
			}
		})
	}
}

func signedHelperRequest(secret []byte, now time.Time) control.Request {
	request := control.Request{
		Version:   control.ProtocolVersion,
		RequestID: "0123456789abcdef0123456789abcdef",
		Operation: control.RestartStarPilot,
		Reason:    "remote StarPilot restart",
		Timestamp: now,
	}
	request.Signature = control.Sign(secret, request)
	return request
}

func sendHelperRequest(
	t *testing.T,
	server *Server,
	request control.Request,
) control.Response {
	t.Helper()
	clientConnection, serverConnection := net.Pipe()
	defer clientConnection.Close()
	done := make(chan struct{})
	go func() {
		server.handle(context.Background(), serverConnection)
		close(done)
	}()
	writeDone := make(chan error, 1)
	go func() {
		writeDone <- json.NewEncoder(clientConnection).Encode(request)
	}()
	var response control.Response
	if err := json.NewDecoder(clientConnection).Decode(&response); err != nil {
		t.Fatal(err)
	}
	_ = clientConnection.Close()
	<-writeDone
	<-done
	return response
}

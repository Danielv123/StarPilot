package control

import (
	"strings"
	"testing"
	"time"
)

func TestControlRequestSignatureBindsEveryImmutableField(t *testing.T) {
	secret := []byte("0123456789abcdef0123456789abcdef")
	request := Request{
		Version:   ProtocolVersion,
		RequestID: "0123456789abcdef0123456789abcdef",
		Operation: RestartStarPilot,
		Reason:    "remote StarPilot restart",
		Timestamp: time.Date(2026, 7, 29, 12, 0, 0, 0, time.UTC),
	}
	request.Signature = Sign(secret, request)
	if err := ValidateRequest(request, request.Timestamp, time.Second); err != nil {
		t.Fatal(err)
	}
	if !Authenticate(secret, request) {
		t.Fatal("valid control request did not authenticate")
	}
	mutations := []func(*Request){
		func(value *Request) { value.RequestID = "fedcba9876543210fedcba9876543210" },
		func(value *Request) { value.Operation = RebootDevice },
		func(value *Request) { value.Reason = "different" },
		func(value *Request) { value.Timestamp = value.Timestamp.Add(time.Second) },
	}
	for index, mutate := range mutations {
		changed := request
		mutate(&changed)
		if Authenticate(secret, changed) {
			t.Fatalf("mutation %d retained a valid signature", index)
		}
	}
}

func TestValidateControlRequestRejectsMalformedOrStaleInput(t *testing.T) {
	now := time.Date(2026, 7, 29, 12, 0, 0, 0, time.UTC)
	valid := Request{
		Version:   ProtocolVersion,
		RequestID: "0123456789abcdef0123456789abcdef",
		Operation: ShutdownDevice,
		Reason:    "owner requested shutdown",
		Timestamp: now,
		Signature: strings.Repeat("a", 64),
	}
	tests := []func(*Request){
		func(value *Request) { value.Version++ },
		func(value *Request) { value.RequestID = strings.ToUpper(value.RequestID) },
		func(value *Request) { value.Operation = "shell" },
		func(value *Request) { value.Reason = "line one\nline two" },
		func(value *Request) { value.Timestamp = now.Add(-time.Minute) },
		func(value *Request) { value.Signature = "not-a-signature" },
	}
	for index, mutate := range tests {
		request := valid
		mutate(&request)
		if err := ValidateRequest(request, now, 30*time.Second); err == nil {
			t.Fatalf("invalid control request %d was accepted", index)
		}
	}
}

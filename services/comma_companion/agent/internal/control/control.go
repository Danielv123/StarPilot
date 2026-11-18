package control

import (
	"bufio"
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"strings"
	"time"
)

const ProtocolVersion = 1

type Operation string

const (
	RestartStarPilot Operation = "restart_starpilot"
	RebootDevice     Operation = "reboot_device"
	ShutdownDevice   Operation = "shutdown_device"
)

type Request struct {
	Version   int       `json:"version"`
	RequestID string    `json:"request_id"`
	Operation Operation `json:"operation"`
	Reason    string    `json:"reason"`
	Timestamp time.Time `json:"timestamp"`
	Signature string    `json:"signature"`
}

type Response struct {
	RequestID string `json:"request_id"`
	State     string `json:"state"`
	Message   string `json:"message,omitempty"`
	Error     string `json:"error,omitempty"`
}

type Client struct {
	socketPath string
	secret     []byte
	timeout    time.Duration
	now        func() time.Time
}

func NewClient(socketPath, secret string, timeout time.Duration) *Client {
	if timeout <= 0 {
		timeout = 30 * time.Second
	}
	return &Client{
		socketPath: socketPath,
		secret:     []byte(secret),
		timeout:    timeout,
		now:        time.Now,
	}
}

func (c *Client) Execute(
	ctx context.Context,
	requestID string,
	operation Operation,
	reason string,
) error {
	request := Request{
		Version:   ProtocolVersion,
		RequestID: requestID,
		Operation: operation,
		Reason:    reason,
		Timestamp: c.now().UTC(),
	}
	request.Signature = Sign(c.secret, request)
	if err := ValidateRequest(request, request.Timestamp, 0); err != nil {
		return err
	}
	ctx, cancel := context.WithTimeout(ctx, c.timeout)
	defer cancel()
	connection, err := (&net.Dialer{}).DialContext(ctx, "unix", c.socketPath)
	if err != nil {
		return fmt.Errorf("connect privileged control helper: %w", err)
	}
	defer connection.Close()
	if deadline, ok := ctx.Deadline(); ok {
		_ = connection.SetDeadline(deadline)
	}
	encoder := json.NewEncoder(connection)
	if err := encoder.Encode(request); err != nil {
		return fmt.Errorf("send privileged control request: %w", err)
	}
	decoder := json.NewDecoder(io.LimitReader(bufio.NewReader(connection), 64<<10))
	decoder.DisallowUnknownFields()
	var response Response
	if err := decoder.Decode(&response); err != nil {
		return fmt.Errorf("read privileged control response: %w", err)
	}
	var trailing any
	if err := decoder.Decode(&trailing); !errors.Is(err, io.EOF) {
		return errors.New("privileged control helper returned trailing data")
	}
	if response.RequestID != requestID {
		return errors.New("privileged control helper response request_id mismatch")
	}
	if response.State != "succeeded" {
		if response.Error == "" {
			response.Error = "privileged control helper rejected the action"
		}
		return errors.New(response.Error)
	}
	return nil
}

func Sign(secret []byte, request Request) string {
	mac := hmac.New(sha256.New, secret)
	_, _ = mac.Write(signingBytes(request))
	return hex.EncodeToString(mac.Sum(nil))
}

func Authenticate(secret []byte, request Request) bool {
	provided, err := hex.DecodeString(request.Signature)
	if err != nil || len(provided) != sha256.Size {
		return false
	}
	expected, err := hex.DecodeString(Sign(secret, request))
	return err == nil && hmac.Equal(provided, expected)
}

func ValidateRequest(request Request, now time.Time, maximumClockSkew time.Duration) error {
	if request.Version != ProtocolVersion {
		return errors.New("unsupported privileged control protocol version")
	}
	if len(request.RequestID) != 32 ||
		request.RequestID != strings.ToLower(request.RequestID) {
		return errors.New("control request_id must be 32 lowercase hexadecimal characters")
	}
	if decoded, err := hex.DecodeString(request.RequestID); err != nil || len(decoded) != 16 {
		return errors.New("control request_id must be 32 lowercase hexadecimal characters")
	}
	switch request.Operation {
	case RestartStarPilot, RebootDevice, ShutdownDevice:
	default:
		return errors.New("unsupported privileged control operation")
	}
	if request.Reason != strings.TrimSpace(request.Reason) ||
		request.Reason == "" || len(request.Reason) > 256 ||
		strings.ContainsAny(request.Reason, "\r\n\x00") {
		return errors.New("control reason must be one line containing 1 through 256 bytes")
	}
	if request.Timestamp.IsZero() {
		return errors.New("control timestamp is required")
	}
	if maximumClockSkew > 0 {
		delta := now.UTC().Sub(request.Timestamp.UTC())
		if delta < -maximumClockSkew || delta > maximumClockSkew {
			return errors.New("control timestamp is outside the accepted clock window")
		}
	}
	if len(request.Signature) != sha256.Size*2 ||
		request.Signature != strings.ToLower(request.Signature) {
		return errors.New("control signature must be lowercase SHA-256 hex")
	}
	return nil
}

func signingBytes(request Request) []byte {
	return []byte(fmt.Sprintf(
		"%d\x00%s\x00%s\x00%s\x00%s",
		request.Version,
		request.RequestID,
		request.Operation,
		request.Reason,
		request.Timestamp.UTC().Format(time.RFC3339Nano),
	))
}

//go:build !linux

package controlhelper

import (
	"context"
	"errors"
	"net"
	"os"

	"starpilot.local/comma-companion-agent/internal/control"
)

type PeerPolicy struct {
	AllowedUID uint32
	CgroupUnit string
	Executable string
}

func RunningAsRoot() bool {
	return false
}

func VerifyPeer(PeerPolicy) PeerValidator {
	return func(net.Conn) error {
		return errors.New("privileged control peer verification requires Linux")
	}
}

func ListenUnix(string, int) (net.Listener, func(), error) {
	return nil, nil, errors.New("privileged control helper requires Linux")
}

func ProductionExecutor(string) Executor {
	return func(context.Context, control.Operation, string) error {
		return errors.New("privileged control actions require Linux")
	}
}

func ReadRootSecret(string) ([]byte, error) {
	return nil, errors.New("privileged control secret loading requires Linux")
}

func validateRootOwnedProof(os.FileInfo) error {
	return nil
}

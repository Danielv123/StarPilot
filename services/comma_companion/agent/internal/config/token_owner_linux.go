//go:build linux

package config

import (
	"errors"
	"os"
	"syscall"
)

func validateTokenOwner(info os.FileInfo) error {
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !ok {
		return errors.New("cannot determine token_file owner")
	}
	effectiveUID := uint32(os.Geteuid())
	if stat.Uid != effectiveUID && stat.Uid != 0 {
		return errors.New("token_file must be owned by the service user or root")
	}
	return nil
}

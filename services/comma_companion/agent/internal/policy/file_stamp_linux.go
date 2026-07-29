//go:build linux

package policy

import (
	"errors"
	"fmt"
	"os"
	"syscall"
)

func fileStamp(info os.FileInfo) (string, error) {
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !ok {
		return "", errors.New("cannot determine policy file identity")
	}
	return fmt.Sprintf(
		"%d:%d:%d:%d:%d",
		stat.Dev,
		stat.Ino,
		info.Size(),
		info.ModTime().UnixNano(),
		info.Mode().Perm(),
	), nil
}

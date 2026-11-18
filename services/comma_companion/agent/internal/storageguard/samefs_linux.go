//go:build linux

package storageguard

import (
	"fmt"
	"os"
	"syscall"
)

func SameFilesystem(left, right string) (bool, error) {
	leftInfo, err := os.Stat(left)
	if err != nil {
		return false, err
	}
	rightInfo, err := os.Stat(right)
	if err != nil {
		return false, err
	}
	leftStat, leftOK := leftInfo.Sys().(*syscall.Stat_t)
	rightStat, rightOK := rightInfo.Sys().(*syscall.Stat_t)
	if !leftOK || !rightOK {
		return false, fmt.Errorf("filesystem device metadata unavailable")
	}
	return leftStat.Dev == rightStat.Dev, nil
}

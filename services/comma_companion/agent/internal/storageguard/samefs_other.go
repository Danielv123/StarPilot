//go:build !linux

package storageguard

import (
	"path/filepath"
	"strings"
)

func SameFilesystem(left, right string) (bool, error) {
	return strings.EqualFold(filepath.VolumeName(left), filepath.VolumeName(right)), nil
}

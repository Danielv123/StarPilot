//go:build !linux

package policy

import (
	"fmt"
	"os"
)

func fileStamp(info os.FileInfo) (string, error) {
	return fmt.Sprintf(
		"%d:%d:%d",
		info.Size(),
		info.ModTime().UnixNano(),
		info.Mode().Perm(),
	), nil
}

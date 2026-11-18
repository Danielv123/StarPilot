//go:build !linux

package config

import "os"

func validateTokenOwner(os.FileInfo) error {
	return nil
}

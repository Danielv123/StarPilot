//go:build !linux

package storageguard

import "math"

func freeBytes(_ string) (int64, error) {
	// Unit tests on non-Linux hosts still exercise logical retention limits.
	// The production target is Linux, where Statfs supplies the real value.
	return math.MaxInt64, nil
}

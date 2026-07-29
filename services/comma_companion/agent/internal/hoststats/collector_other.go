//go:build !linux

package hoststats

import "time"

type Collector struct{}

func New() *Collector {
	return &Collector{}
}

func (c *Collector) Collect() Snapshot {
	return Snapshot{CollectedAt: time.Now().UTC()}
}

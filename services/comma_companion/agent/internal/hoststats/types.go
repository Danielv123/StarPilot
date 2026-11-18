package hoststats

import "time"

type Temperature struct {
	Name    string  `json:"name"`
	Celsius float64 `json:"celsius"`
}

type NetworkInterface struct {
	Name    string `json:"name"`
	RXBytes int64  `json:"rx_bytes"`
	TXBytes int64  `json:"tx_bytes"`
}

type PowerSupply struct {
	Name    string           `json:"name"`
	Type    string           `json:"type,omitempty"`
	Status  string           `json:"status,omitempty"`
	Metrics map[string]int64 `json:"metrics,omitempty"`
}

type Repository struct {
	Branch string `json:"branch,omitempty"`
	Commit string `json:"commit,omitempty"`
}

type Snapshot struct {
	CollectedAt           time.Time          `json:"collected_at"`
	UptimeSeconds         float64            `json:"uptime_seconds,omitempty"`
	Load1                 float64            `json:"load_1,omitempty"`
	Load5                 float64            `json:"load_5,omitempty"`
	Load15                float64            `json:"load_15,omitempty"`
	CPUUtilizationPercent float64            `json:"cpu_utilization_percent,omitempty"`
	MemoryTotalBytes      int64              `json:"memory_total_bytes,omitempty"`
	MemoryAvailableBytes  int64              `json:"memory_available_bytes,omitempty"`
	DataTotalBytes        int64              `json:"data_total_bytes,omitempty"`
	DataFreeBytes         int64              `json:"data_free_bytes,omitempty"`
	Temperatures          []Temperature      `json:"temperatures,omitempty"`
	NetworkInterfaces     []NetworkInterface `json:"network_interfaces,omitempty"`
	PowerSupplies         []PowerSupply      `json:"power_supplies,omitempty"`
	StarPilot             Repository         `json:"starpilot"`
	CurrentRoute          string             `json:"current_route,omitempty"`
	ManagerRunning        bool               `json:"manager_running"`
	LoggerdRunning        bool               `json:"loggerd_running"`
}

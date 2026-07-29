//go:build linux

package hoststats

import (
	"bufio"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
)

const (
	maxListEntries = 16
	maxProcessScan = 4096
)

type Collector struct {
	mu        sync.Mutex
	lastTotal uint64
	lastIdle  uint64
}

func New() *Collector {
	return &Collector{}
}

func (c *Collector) Collect() Snapshot {
	result := Snapshot{CollectedAt: time.Now().UTC()}
	result.UptimeSeconds = firstFloat(readSmall("/proc/uptime"))
	load := floatFields(readSmall("/proc/loadavg"))
	if len(load) >= 3 {
		result.Load1, result.Load5, result.Load15 = load[0], load[1], load[2]
	}
	total, idle := cpuTimes(readSmall("/proc/stat"))
	c.mu.Lock()
	if c.lastTotal > 0 && total > c.lastTotal {
		deltaTotal := total - c.lastTotal
		deltaIdle := idle - c.lastIdle
		if deltaIdle <= deltaTotal {
			result.CPUUtilizationPercent = 100 * (1 - float64(deltaIdle)/float64(deltaTotal))
		}
	}
	c.lastTotal, c.lastIdle = total, idle
	c.mu.Unlock()
	result.MemoryTotalBytes, result.MemoryAvailableBytes = memory(readSmall("/proc/meminfo"))
	result.DataTotalBytes, result.DataFreeBytes = disk("/data")
	result.Temperatures = temperatures("/sys/class/thermal")
	result.NetworkInterfaces = network("/sys/class/net")
	result.PowerSupplies = power("/sys/class/power_supply")
	result.CurrentRoute = bounded(strings.TrimSpace(readSmall("/data/params/d/CurrentRoute")), 256)
	result.StarPilot = repository("/data/openpilot")
	result.ManagerRunning, result.LoggerdRunning = processes("/proc")
	return result
}

func readSmall(path string) string {
	return readLimit(path, 64*1024)
}

func readLimit(path string, limit int) string {
	file, err := os.Open(path)
	if err != nil {
		return ""
	}
	defer file.Close()
	buffer := make([]byte, limit)
	count, _ := file.Read(buffer)
	return string(buffer[:count])
}

func firstFloat(value string) float64 {
	fields := strings.Fields(value)
	if len(fields) == 0 {
		return 0
	}
	result, _ := strconv.ParseFloat(fields[0], 64)
	return result
}

func floatFields(value string) []float64 {
	fields := strings.Fields(value)
	result := make([]float64, 0, len(fields))
	for _, field := range fields {
		parsed, err := strconv.ParseFloat(field, 64)
		if err != nil {
			break
		}
		result = append(result, parsed)
	}
	return result
}

func cpuTimes(value string) (uint64, uint64) {
	line, _, _ := strings.Cut(value, "\n")
	fields := strings.Fields(line)
	if len(fields) < 5 || fields[0] != "cpu" {
		return 0, 0
	}
	var total uint64
	values := make([]uint64, 0, len(fields)-1)
	for _, field := range fields[1:] {
		parsed, err := strconv.ParseUint(field, 10, 64)
		if err != nil {
			return 0, 0
		}
		values = append(values, parsed)
		total += parsed
	}
	idle := values[3]
	if len(values) > 4 {
		idle += values[4]
	}
	return total, idle
}

func memory(value string) (int64, int64) {
	var total, available int64
	scanner := bufio.NewScanner(strings.NewReader(value))
	for scanner.Scan() {
		fields := strings.Fields(scanner.Text())
		if len(fields) < 2 {
			continue
		}
		number, err := strconv.ParseInt(fields[1], 10, 64)
		if err != nil {
			continue
		}
		switch strings.TrimSuffix(fields[0], ":") {
		case "MemTotal":
			total = number * 1024
		case "MemAvailable":
			available = number * 1024
		}
	}
	return total, available
}

func disk(path string) (int64, int64) {
	var stats syscall.Statfs_t
	if err := syscall.Statfs(path, &stats); err != nil {
		return 0, 0
	}
	return int64(stats.Blocks) * int64(stats.Bsize), int64(stats.Bavail) * int64(stats.Bsize)
}

func temperatures(root string) []Temperature {
	entries, err := sortedEntries(root)
	if err != nil {
		return nil
	}
	result := make([]Temperature, 0)
	for _, entry := range entries {
		if len(result) >= maxListEntries || !strings.HasPrefix(entry.Name(), "thermal_zone") {
			continue
		}
		dir := filepath.Join(root, entry.Name())
		raw, err := strconv.ParseFloat(strings.TrimSpace(readSmall(filepath.Join(dir, "temp"))), 64)
		if err != nil {
			continue
		}
		name := strings.TrimSpace(readSmall(filepath.Join(dir, "type")))
		if name == "" {
			name = entry.Name()
		}
		if raw > 1000 || raw < -1000 {
			raw /= 1000
		}
		result = append(result, Temperature{Name: bounded(name, 64), Celsius: raw})
	}
	return result
}

func network(root string) []NetworkInterface {
	entries, err := sortedEntries(root)
	if err != nil {
		return nil
	}
	result := make([]NetworkInterface, 0)
	for _, entry := range entries {
		if len(result) >= maxListEntries || entry.Name() == "lo" {
			continue
		}
		dir := filepath.Join(root, entry.Name(), "statistics")
		rx, rxErr := parseInt(readSmall(filepath.Join(dir, "rx_bytes")))
		tx, txErr := parseInt(readSmall(filepath.Join(dir, "tx_bytes")))
		if rxErr != nil || txErr != nil {
			continue
		}
		result = append(result, NetworkInterface{Name: entry.Name(), RXBytes: rx, TXBytes: tx})
	}
	return result
}

func power(root string) []PowerSupply {
	entries, err := sortedEntries(root)
	if err != nil {
		return nil
	}
	keys := []string{
		"capacity", "voltage_now", "current_now", "power_now",
		"energy_now", "charge_now", "temp",
	}
	result := make([]PowerSupply, 0)
	for _, entry := range entries {
		if len(result) >= maxListEntries {
			break
		}
		dir := filepath.Join(root, entry.Name())
		supply := PowerSupply{
			Name:    entry.Name(),
			Type:    bounded(strings.TrimSpace(readSmall(filepath.Join(dir, "type"))), 64),
			Status:  bounded(strings.TrimSpace(readSmall(filepath.Join(dir, "status"))), 64),
			Metrics: make(map[string]int64),
		}
		for _, key := range keys {
			if value, err := parseInt(readSmall(filepath.Join(dir, key))); err == nil {
				supply.Metrics[key] = value
			}
		}
		result = append(result, supply)
	}
	return result
}

func repository(worktree string) Repository {
	gitDir := filepath.Join(worktree, ".git")
	if info, err := os.Stat(gitDir); err == nil && !info.IsDir() {
		raw := strings.TrimSpace(readSmall(gitDir))
		if strings.HasPrefix(raw, "gitdir:") {
			gitDir = strings.TrimSpace(strings.TrimPrefix(raw, "gitdir:"))
			if !filepath.IsAbs(gitDir) {
				gitDir = filepath.Join(worktree, gitDir)
			}
		}
	}
	head := strings.TrimSpace(readSmall(filepath.Join(gitDir, "HEAD")))
	if head == "" {
		return Repository{}
	}
	if !strings.HasPrefix(head, "ref: ") {
		return Repository{Commit: validCommit(head)}
	}
	ref := strings.TrimSpace(strings.TrimPrefix(head, "ref: "))
	if !validRef(ref) {
		return Repository{}
	}
	result := Repository{Branch: bounded(strings.TrimPrefix(ref, "refs/heads/"), 128)}
	result.Commit = validCommit(strings.TrimSpace(readSmall(filepath.Join(gitDir, filepath.FromSlash(ref)))))
	if result.Commit == "" {
		result.Commit = validCommit(packedRef(filepath.Join(gitDir, "packed-refs"), ref))
	}
	return result
}

func packedRef(path, target string) string {
	scanner := bufio.NewScanner(strings.NewReader(readSmall(path)))
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" || strings.HasPrefix(line, "#") || strings.HasPrefix(line, "^") {
			continue
		}
		fields := strings.Fields(line)
		if len(fields) == 2 && fields[1] == target {
			return fields[0]
		}
	}
	return ""
}

func processes(root string) (bool, bool) {
	entries, err := sortedEntries(root)
	if err != nil {
		return false, false
	}
	manager, loggerd := false, false
	scanned := 0
	for _, entry := range entries {
		if !isNumeric(entry.Name()) {
			continue
		}
		if scanned >= maxProcessScan {
			break
		}
		scanned++
		command := strings.ReplaceAll(readLimit(filepath.Join(root, entry.Name(), "cmdline"), 4096), "\x00", " ")
		fields := strings.Fields(command)
		if len(fields) == 0 {
			continue
		}
		base := filepath.Base(fields[0])
		if base == "manager" || strings.Contains(command, "manager.py") {
			manager = true
		}
		if base == "loggerd" || strings.Contains(command, "/loggerd") {
			loggerd = true
		}
		if manager && loggerd {
			break
		}
	}
	return manager, loggerd
}

func sortedEntries(path string) ([]os.DirEntry, error) {
	entries, err := os.ReadDir(path)
	if err != nil {
		return nil, err
	}
	sort.Slice(entries, func(i, j int) bool {
		return entries[i].Name() < entries[j].Name()
	})
	return entries, nil
}

func parseInt(value string) (int64, error) {
	return strconv.ParseInt(strings.TrimSpace(value), 10, 64)
}

func isNumeric(value string) bool {
	if value == "" {
		return false
	}
	for _, character := range value {
		if character < '0' || character > '9' {
			return false
		}
	}
	return true
}

func bounded(value string, maximum int) string {
	if len(value) <= maximum {
		return value
	}
	return value[:maximum]
}

func validCommit(value string) string {
	value = strings.TrimSpace(value)
	if len(value) < 7 || len(value) > 64 {
		return ""
	}
	for _, character := range value {
		if (character < '0' || character > '9') &&
			(character < 'a' || character > 'f') &&
			(character < 'A' || character > 'F') {
			return ""
		}
	}
	return value
}

func validRef(value string) bool {
	if !strings.HasPrefix(value, "refs/heads/") || strings.Contains(value, "..") ||
		strings.ContainsAny(value, "\\\x00\r\n") {
		return false
	}
	return len(value) <= 512
}

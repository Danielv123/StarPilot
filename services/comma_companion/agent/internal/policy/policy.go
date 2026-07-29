package policy

import (
	"bufio"
	"io"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"

	"starpilot.local/comma-companion-agent/internal/config"
)

type Status struct {
	Offroad            bool      `json:"offroad"`
	OffroadKnown       bool      `json:"offroad_known"`
	OffroadStable      bool      `json:"offroad_stable"`
	OffroadSourceFresh bool      `json:"offroad_source_fresh"`
	OffroadSince       time.Time `json:"offroad_since,omitempty"`
	NetworkType        string    `json:"network_type"`
	Metered            bool      `json:"metered"`
	MeteredKnown       bool      `json:"metered_known"`
	UploadAllowed      bool      `json:"upload_allowed"`
	BlockedReason      string    `json:"blocked_reason,omitempty"`
	ObservedAt         time.Time `json:"observed_at"`
}

type Reader struct {
	config        config.Policy
	mu            sync.Mutex
	last          Status
	count         int
	offroadSince  time.Time
	offroadStamp  string
	onroadStamp   string
	operstateRoot string
	ipv4Routes    string
	ipv6Routes    string
	now           func() time.Time
}

func New(cfg config.Policy) *Reader {
	return &Reader{
		config:        cfg,
		operstateRoot: "/sys/class/net",
		ipv4Routes:    "/proc/net/route",
		ipv6Routes:    "/proc/net/ipv6_route",
		now:           time.Now,
	}
}

func (r *Reader) Read() Status {
	r.mu.Lock()
	defer r.mu.Unlock()
	now := r.now().UTC()
	offroad, offroadKnown, offroadModified, offroadStamp := readBool(
		r.config.OffroadStateFile,
		r.config.TrueValues,
	)
	onroad, onroadKnown, onroadModified, onroadStamp := readBool(
		r.config.OnroadStateFile,
		r.config.TrueValues,
	)
	if r.config.OnroadStateFile != "" {
		offroadKnown = offroadKnown && onroadKnown && !onroad
	}
	metered, meteredKnown, _, _ := readBool(r.config.MeteredStateFile, r.config.TrueValues)
	networkType := readString(r.config.NetworkTypeFile)
	wifiConfirmed := detectWiFi(
		r.config.WiFiInterface,
		r.operstateRoot,
		r.ipv4Routes,
		r.ipv6Routes,
	) == "wifi"
	if networkType == "" && wifiConfirmed {
		networkType = "wifi"
	}
	if r.config.RequireWiFi && strings.EqualFold(networkType, "wifi") && !wifiConfirmed {
		networkType = "unknown"
	}
	if networkType == "" {
		networkType = "unknown"
	}
	sourceFresh := fresh(now, offroadModified, r.config.OffroadMaxAge.Duration)
	if r.config.OnroadStateFile != "" {
		sourceFresh = sourceFresh && fresh(now, onroadModified, r.config.OffroadMaxAge.Duration)
	}
	sourceChanged := (r.offroadStamp != "" && offroadStamp != r.offroadStamp) ||
		(r.config.OnroadStateFile != "" && r.onroadStamp != "" && onroadStamp != r.onroadStamp)
	if offroadKnown && offroad && sourceFresh && !sourceChanged && r.last.OffroadKnown &&
		r.last.Offroad && r.last.OffroadSourceFresh {
		r.count++
	} else if offroadKnown && offroad {
		r.count = 1
		r.offroadSince = now
	} else {
		r.count = 0
		r.offroadSince = time.Time{}
	}
	r.offroadStamp = offroadStamp
	r.onroadStamp = onroadStamp
	stableDuration := r.config.OffroadStableDuration.Duration
	status := Status{
		Offroad:      offroad,
		OffroadKnown: offroadKnown,
		OffroadStable: offroadKnown && offroad && sourceFresh && r.count >= 2 &&
			!r.offroadSince.IsZero() && now.Sub(r.offroadSince) >= stableDuration,
		OffroadSourceFresh: sourceFresh,
		OffroadSince:       r.offroadSince,
		NetworkType:        strings.ToLower(networkType),
		Metered:            metered,
		MeteredKnown:       meteredKnown,
		UploadAllowed:      true,
		ObservedAt:         now,
	}
	switch {
	case r.config.UploadOnlyOffroad && (!offroadKnown || !offroad || !sourceFresh):
		status.UploadAllowed = false
		status.BlockedReason = "waiting for a confirmed offroad state"
	case r.config.RequireWiFi && status.NetworkType != "wifi":
		status.UploadAllowed = false
		status.BlockedReason = "waiting for WiFi"
	case !r.config.AllowMetered && r.config.MeteredStateFile != "" && !meteredKnown:
		status.UploadAllowed = false
		status.BlockedReason = "metered-network state is unknown"
	case !r.config.AllowMetered && meteredKnown && metered:
		status.UploadAllowed = false
		status.BlockedReason = "metered network is disabled"
	}
	r.last = status
	return status
}

func (r *Reader) PowerActionAllowed() (bool, string) {
	status := r.Read()
	if !status.OffroadKnown {
		return false, "offroad state is unknown"
	}
	if !status.Offroad {
		return false, "device is onroad"
	}
	if !status.OffroadSourceFresh {
		return false, "offroad state files are stale"
	}
	if !status.OffroadStable {
		return false, "offroad state has not been stable for the required duration"
	}
	return true, ""
}

func readBool(path string, trueValues []string) (bool, bool, time.Time, string) {
	if strings.TrimSpace(path) == "" {
		return false, false, time.Time{}, ""
	}
	cleaned := filepath.Clean(path)
	handle, err := os.Open(cleaned)
	if err != nil {
		return false, false, time.Time{}, ""
	}
	defer handle.Close()
	before, err := handle.Stat()
	if err != nil {
		return false, false, time.Time{}, ""
	}
	raw, err := io.ReadAll(io.LimitReader(handle, 4097))
	if err != nil || len(raw) > 4096 {
		return false, false, time.Time{}, ""
	}
	after, err := handle.Stat()
	if err != nil || !os.SameFile(before, after) ||
		before.Size() != after.Size() ||
		before.ModTime().UnixNano() != after.ModTime().UnixNano() {
		return false, false, time.Time{}, ""
	}
	pathInfo, err := os.Stat(cleaned)
	if err != nil || !os.SameFile(after, pathInfo) {
		return false, false, time.Time{}, ""
	}
	stamp, err := fileStamp(after)
	if err != nil {
		return false, false, time.Time{}, ""
	}
	value := strings.ToLower(strings.TrimSpace(string(raw)))
	for _, candidate := range trueValues {
		if value == strings.ToLower(strings.TrimSpace(candidate)) {
			return true, true, after.ModTime(), stamp
		}
	}
	switch value {
	case "0", "false", "no", "off":
		return false, true, after.ModTime(), stamp
	default:
		return false, false, after.ModTime(), stamp
	}
}

func fresh(now, modified time.Time, maxAge time.Duration) bool {
	if modified.IsZero() {
		return false
	}
	if maxAge <= 0 {
		return true
	}
	age := now.Sub(modified)
	return age >= -time.Minute && age <= maxAge
}

func readString(path string) string {
	if strings.TrimSpace(path) == "" {
		return ""
	}
	raw, err := os.ReadFile(filepath.Clean(path))
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(raw))
}

func detectWiFi(interfaceName, operstateRoot, ipv4RoutePath, ipv6RoutePath string) string {
	if strings.TrimSpace(interfaceName) == "" {
		return ""
	}
	interfaceName = filepath.Base(interfaceName)
	raw, err := os.ReadFile(filepath.Join(operstateRoot, interfaceName, "operstate"))
	if err != nil {
		return ""
	}
	if strings.TrimSpace(string(raw)) != "up" {
		return ""
	}
	routes, valid := selectedDefaultRouteInterfaces(ipv4RoutePath, ipv6RoutePath)
	if !valid || len(routes) == 0 {
		return ""
	}
	for _, routeInterface := range routes {
		if routeInterface != interfaceName {
			return ""
		}
	}
	return "wifi"
}

type defaultRoute struct {
	interfaceName string
	metric        uint64
}

func selectedDefaultRouteInterfaces(ipv4Path, ipv6Path string) ([]string, bool) {
	interfaces := make([]string, 0)
	validTable := false
	if raw := readString(ipv4Path); raw != "" {
		routes, ok := ipv4DefaultRoutes(raw)
		if !ok {
			return nil, false
		}
		validTable = true
		interfaces = append(interfaces, lowestMetricInterfaces(routes)...)
	}
	if raw := readString(ipv6Path); raw != "" {
		routes, ok := ipv6DefaultRoutes(raw)
		if !ok {
			return nil, false
		}
		validTable = true
		interfaces = append(interfaces, lowestMetricInterfaces(routes)...)
	}
	return interfaces, validTable
}

func lowestMetricInterfaces(routes []defaultRoute) []string {
	if len(routes) == 0 {
		return nil
	}
	lowest := routes[0].metric
	for _, route := range routes[1:] {
		if route.metric < lowest {
			lowest = route.metric
		}
	}
	interfaces := make([]string, 0, len(routes))
	for _, route := range routes {
		if route.metric == lowest {
			interfaces = append(interfaces, route.interfaceName)
		}
	}
	return interfaces
}

func ipv4DefaultRoutes(raw string) ([]defaultRoute, bool) {
	if strings.TrimSpace(raw) == "" {
		return nil, false
	}
	scanner := bufio.NewScanner(strings.NewReader(raw))
	routes := make([]defaultRoute, 0)
	headerSeen := false
	for scanner.Scan() {
		fields := strings.Fields(scanner.Text())
		if !headerSeen {
			headerSeen = len(fields) >= 2 && fields[0] == "Iface"
			if !headerSeen {
				return nil, false
			}
			continue
		}
		if len(fields) < 8 || fields[1] != "00000000" || fields[7] != "00000000" {
			continue
		}
		flags, err := strconv.ParseUint(fields[3], 16, 32)
		if err != nil {
			return nil, false
		}
		if flags&1 != 0 {
			metric, err := strconv.ParseUint(fields[6], 10, 64)
			if err != nil {
				return nil, false
			}
			routes = append(routes, defaultRoute{
				interfaceName: fields[0],
				metric:        metric,
			})
		}
	}
	if err := scanner.Err(); err != nil {
		return nil, false
	}
	return routes, true
}

func ipv6DefaultRoutes(raw string) ([]defaultRoute, bool) {
	if strings.TrimSpace(raw) == "" {
		return nil, false
	}
	scanner := bufio.NewScanner(strings.NewReader(raw))
	routes := make([]defaultRoute, 0)
	for scanner.Scan() {
		fields := strings.Fields(scanner.Text())
		if len(fields) != 10 {
			return nil, false
		}
		if fields[0] != strings.Repeat("0", 32) || fields[1] != "00" {
			continue
		}
		flags, err := strconv.ParseUint(fields[8], 16, 32)
		if err != nil {
			return nil, false
		}
		if flags&1 != 0 {
			metric, err := strconv.ParseUint(fields[5], 16, 64)
			if err != nil {
				return nil, false
			}
			routes = append(routes, defaultRoute{
				interfaceName: fields[9],
				metric:        metric,
			})
		}
	}
	if err := scanner.Err(); err != nil {
		return nil, false
	}
	return routes, true
}

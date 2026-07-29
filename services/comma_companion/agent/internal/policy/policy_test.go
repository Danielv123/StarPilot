package policy

import (
	"os"
	"path/filepath"
	"testing"
	"time"

	"starpilot.local/comma-companion-agent/internal/config"
)

func TestOffroadPowerGateRequiresTwoObservations(t *testing.T) {
	dir := t.TempDir()
	offroad := filepath.Join(dir, "IsOffroad")
	metered := filepath.Join(dir, "NetworkMetered")
	network := filepath.Join(dir, "NetworkType")
	write(t, offroad, "1")
	write(t, metered, "0")
	write(t, network, "wifi")
	reader := New(config.Policy{
		UploadOnlyOffroad:     true,
		RequireWiFi:           false,
		NetworkTypeFile:       network,
		MeteredStateFile:      metered,
		OffroadStateFile:      offroad,
		TrueValues:            []string{"1", "true"},
		OffroadStableDuration: config.Duration{Duration: 10 * time.Second},
		OffroadMaxAge:         config.Duration{Duration: time.Hour},
	})
	now := time.Now().UTC()
	reader.now = func() time.Time { return now }
	first := reader.Read()
	if !first.UploadAllowed || first.OffroadStable {
		t.Fatalf("unexpected first status: %#v", first)
	}
	now = now.Add(5 * time.Second)
	if allowed, _ := reader.PowerActionAllowed(); allowed {
		t.Fatal("rapid repeated reads bypassed elapsed offroad gate")
	}
	now = now.Add(6 * time.Second)
	allowed, reason := reader.PowerActionAllowed()
	if !allowed {
		t.Fatalf("stable elapsed offroad state should permit power action: %s", reason)
	}
	write(t, offroad, "0")
	if allowed, _ := reader.PowerActionAllowed(); allowed {
		t.Fatal("onroad state permitted power action")
	}
}

func TestMeteredNetworkBlocksUpload(t *testing.T) {
	dir := t.TempDir()
	metered := filepath.Join(dir, "metered")
	write(t, metered, "1")
	status := New(config.Policy{
		MeteredStateFile: metered,
		TrueValues:       []string{"1"},
	}).Read()
	if status.UploadAllowed || status.BlockedReason == "" {
		t.Fatalf("metered network was not blocked: %#v", status)
	}
}

func TestOffroadStableWindowResetsWhenStateFileIdentityChanges(t *testing.T) {
	dir := t.TempDir()
	offroad := filepath.Join(dir, "IsOffroad")
	write(t, offroad, "1")
	reader := New(config.Policy{
		OffroadStateFile:      offroad,
		OffroadStableDuration: config.Duration{Duration: 10 * time.Second},
		OffroadMaxAge:         config.Duration{Duration: time.Hour},
		TrueValues:            []string{"1"},
	})
	now := time.Now().UTC()
	reader.now = func() time.Time { return now }
	reader.Read()
	now = now.Add(11 * time.Second)
	if allowed, reason := reader.PowerActionAllowed(); !allowed {
		t.Fatalf("expected stable offroad state: %s", reason)
	}

	replacement := filepath.Join(dir, "replacement")
	write(t, replacement, "1")
	if err := os.Chtimes(replacement, now.Add(time.Second), now.Add(time.Second)); err != nil {
		t.Fatal(err)
	}
	if err := os.Rename(replacement, offroad); err != nil {
		t.Fatal(err)
	}
	now = now.Add(time.Second)
	if allowed, _ := reader.PowerActionAllowed(); allowed {
		t.Fatal("replaced state file inherited the prior stable window")
	}
}

func TestWiFiRequiresEverySelectedDefaultRouteToUseConfiguredInterface(t *testing.T) {
	dir := t.TempDir()
	operstateRoot := filepath.Join(dir, "net")
	if err := os.MkdirAll(filepath.Join(operstateRoot, "wlan0"), 0o700); err != nil {
		t.Fatal(err)
	}
	write(t, filepath.Join(operstateRoot, "wlan0", "operstate"), "up")
	ipv4 := filepath.Join(dir, "route")
	ipv6 := filepath.Join(dir, "ipv6_route")
	write(t, ipv6, "")
	write(t, ipv4, `Iface Destination Gateway Flags RefCnt Use Metric Mask MTU Window IRTT
wlan0 00000000 0100000A 0003 0 0 100 00000000 0 0 0
`)
	if got := detectWiFi("wlan0", operstateRoot, ipv4, ipv6); got != "wifi" {
		t.Fatalf("wifi default route was not recognized: %q", got)
	}
	write(t, ipv4, `Iface Destination Gateway Flags RefCnt Use Metric Mask MTU Window IRTT
wlan0 00000000 0100000A 0003 0 0 100 00000000 0 0 0
wwan0 00000000 0200000A 0003 0 0 200 00000000 0 0 0
`)
	if got := detectWiFi("wlan0", operstateRoot, ipv4, ipv6); got != "wifi" {
		t.Fatalf("higher-metric cellular fallback blocked selected WiFi route: %q", got)
	}
	write(t, ipv4, `Iface Destination Gateway Flags RefCnt Use Metric Mask MTU Window IRTT
wlan0 00000000 0100000A 0003 0 0 600 00000000 0 0 0
wwan0 00000000 0200000A 0003 0 0 1000 00000000 0 0 0
`)
	if got := detectWiFi("wlan0", operstateRoot, ipv4, ipv6); got != "wifi" {
		t.Fatalf("comma-style higher-metric cellular fallback blocked WiFi: %q", got)
	}
	write(t, ipv4, `Iface Destination Gateway Flags RefCnt Use Metric Mask MTU Window IRTT
wlan0 00000000 0100000A 0003 0 0 200 00000000 0 0 0
wwan0 00000000 0200000A 0003 0 0 100 00000000 0 0 0
`)
	if got := detectWiFi("wlan0", operstateRoot, ipv4, ipv6); got != "" {
		t.Fatalf("selected lower-metric cellular route was accepted as WiFi: %q", got)
	}
	write(t, ipv4, `Iface Destination Gateway Flags RefCnt Use Metric Mask MTU Window IRTT
wlan0 00000000 0100000A 0003 0 0 100 00000000 0 0 0
wwan0 00000000 0200000A 0003 0 0 100 00000000 0 0 0
`)
	if got := detectWiFi("wlan0", operstateRoot, ipv4, ipv6); got != "" {
		t.Fatalf("equal-cost cellular route was accepted as unambiguous WiFi: %q", got)
	}
	write(t, ipv4, `Iface Destination Gateway Flags RefCnt Use Metric Mask MTU Window IRTT
wlan0 00000000 0100000A 0003 0 0 100 00000000 0 0 0
`)
	write(t, ipv6, `00000000000000000000000000000000 00 00000000000000000000000000000000 00 00000000000000000000000000000000 00000064 00000000 00000000 00000001 wlan0
00000000000000000000000000000000 00 00000000000000000000000000000000 00 00000000000000000000000000000000 00000032 00000000 00000000 00000001 wwan0
`)
	if got := detectWiFi("wlan0", operstateRoot, ipv4, ipv6); got != "" {
		t.Fatalf("selected IPv6 cellular route was accepted as WiFi: %q", got)
	}
	write(t, filepath.Join(operstateRoot, "wlan0", "operstate"), "dormant")
	write(t, ipv6, "")
	write(t, ipv4, `Iface Destination Gateway Flags RefCnt Use Metric Mask MTU Window IRTT
wlan0 00000000 0100000A 0003 0 0 100 00000000 0 0 0
`)
	if got := detectWiFi("wlan0", operstateRoot, ipv4, ipv6); got != "" {
		t.Fatalf("dormant interface was accepted as WiFi: %q", got)
	}
}

func TestReportedWiFiCannotOverrideDefaultRouteGate(t *testing.T) {
	dir := t.TempDir()
	networkType := filepath.Join(dir, "network-type")
	write(t, networkType, "wifi")
	operstateRoot := filepath.Join(dir, "net")
	if err := os.MkdirAll(filepath.Join(operstateRoot, "wlan0"), 0o700); err != nil {
		t.Fatal(err)
	}
	write(t, filepath.Join(operstateRoot, "wlan0", "operstate"), "up")
	ipv4 := filepath.Join(dir, "route")
	write(t, ipv4, `Iface Destination Gateway Flags RefCnt Use Metric Mask MTU Window IRTT
wwan0 00000000 0200000A 0003 0 0 100 00000000 0 0 0
`)
	reader := New(config.Policy{
		RequireWiFi:     true,
		WiFiInterface:   "wlan0",
		NetworkTypeFile: networkType,
	})
	reader.operstateRoot = operstateRoot
	reader.ipv4Routes = ipv4
	reader.ipv6Routes = filepath.Join(dir, "missing-ipv6")
	status := reader.Read()
	if status.UploadAllowed || status.NetworkType == "wifi" {
		t.Fatalf("reported WiFi bypassed route confirmation: %#v", status)
	}
}

func write(t *testing.T, path, value string) {
	t.Helper()
	if err := os.WriteFile(path, []byte(value), 0o600); err != nil {
		t.Fatal(err)
	}
}

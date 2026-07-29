package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"log"
	"os"
	"os/signal"
	"os/user"
	"path/filepath"
	"strconv"
	"syscall"
	"time"

	"starpilot.local/comma-companion-agent/internal/controlhelper"
)

var version = "dev"

func main() {
	if err := run(); err != nil && !errors.Is(err, context.Canceled) {
		log.Printf("fatal: %v", err)
		os.Exit(1)
	}
}

func run() error {
	showVersion := flag.Bool("version", false, "print helper version")
	socketPath := flag.String(
		"socket",
		"/run/comma-companion-control/control.sock",
		"Unix control socket path",
	)
	secretPath := flag.String(
		"secret-file",
		"/run/credentials/comma-companion-control-helper.service/control-token",
		"root-owned HMAC credential",
	)
	ledgerPath := flag.String(
		"ledger",
		"/data/private-pond-agent-control/control-ledger.json",
		"root-only idempotency ledger",
	)
	agentExecutable := flag.String(
		"agent-executable",
		"/data/private-pond-agent/current/comma-companion-agent",
		"exact executable allowed to connect",
	)
	agentUnit := flag.String(
		"agent-unit",
		"comma-companion-agent.service",
		"exact systemd cgroup unit allowed to connect",
	)
	agentUser := flag.String("agent-user", "comma", "local archive-agent user")
	paramsBase := flag.String("params-base", "/data/params", "Params base directory")
	offroadPath := flag.String(
		"offroad-path",
		"/data/params/d/IsOffroad",
		"IsOffroad Params path",
	)
	onroadPath := flag.String(
		"onroad-path",
		"/data/params/d/IsOnroad",
		"IsOnroad Params path",
	)
	pandaProofPath := flag.String(
		"panda-proof-path",
		"/run/comma-companion-control/panda-ignition.json",
		"root-owned fresh raw panda ignition proof path",
	)
	flag.Parse()
	if *showVersion {
		fmt.Println(version)
		return nil
	}
	if !controlhelper.RunningAsRoot() {
		return errors.New("privileged control helper must run as root")
	}
	account, err := user.Lookup(*agentUser)
	if err != nil {
		return fmt.Errorf("resolve archive-agent user: %w", err)
	}
	uid, err := strconv.ParseUint(account.Uid, 10, 32)
	if err != nil {
		return fmt.Errorf("parse archive-agent UID: %w", err)
	}
	gid, err := strconv.Atoi(account.Gid)
	if err != nil || gid < 0 {
		return errors.New("parse archive-agent group ID")
	}
	if err := prepareLedgerDirectory(filepath.Dir(*ledgerPath)); err != nil {
		return err
	}
	secret, err := controlhelper.ReadRootSecret(*secretPath)
	if err != nil {
		return fmt.Errorf("load privileged control credential: %w", err)
	}
	ledger, err := controlhelper.OpenLedger(*ledgerPath)
	if err != nil {
		return err
	}
	listener, cleanup, err := controlhelper.ListenUnix(*socketPath, gid)
	if err != nil {
		return fmt.Errorf("create privileged control socket: %w", err)
	}
	defer cleanup()
	server := &controlhelper.Server{
		Secret:    secret,
		ClockSkew: 30 * time.Second,
		ValidatePeer: controlhelper.VerifyPeer(controlhelper.PeerPolicy{
			AllowedUID: uint32(uid),
			CgroupUnit: *agentUnit,
			Executable: *agentExecutable,
		}),
		CheckOffroad: controlhelper.OffroadGate{
			OffroadPath:     *offroadPath,
			OnroadPath:      *onroadPath,
			PandaProofPath:  *pandaProofPath,
			StableFor:       10 * time.Second,
			MaximumAge:      24 * time.Hour,
			PandaMaximumAge: 2 * time.Second,
		}.Check,
		Execute: controlhelper.ProductionExecutor(*paramsBase),
		Ledger:  ledger,
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	log.Printf("comma companion privileged control helper %s listening", version)
	return server.Serve(ctx, listener)
}

func prepareLedgerDirectory(path string) error {
	if err := os.MkdirAll(path, 0o700); err != nil {
		return fmt.Errorf("create control state directory: %w", err)
	}
	info, err := os.Lstat(path)
	if err != nil || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
		return errors.New("control state directory must be a non-symlink directory")
	}
	if err := os.Chmod(path, 0o700); err != nil {
		return err
	}
	if err := os.Chown(path, 0, 0); err != nil {
		return err
	}
	return nil
}

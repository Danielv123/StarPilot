package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"log"
	"os"
	"os/signal"
	"syscall"

	"starpilot.local/comma-companion-agent/internal/agent"
	"starpilot.local/comma-companion-agent/internal/config"
	"starpilot.local/comma-companion-agent/internal/policy"
	"starpilot.local/comma-companion-agent/internal/scanner"
)

var version = "dev"

func main() {
	if err := run(); err != nil {
		log.Printf("fatal: %v", err)
		os.Exit(1)
	}
}

func run() error {
	configPath := flag.String("config", "/data/private-pond-agent/config.json", "path to the JSON config")
	showVersion := flag.Bool("version", false, "print the agent version")
	suggestStreams := flag.Bool(
		"suggest-streams",
		false,
		"inspect closed segments and print a suggested inventory expected_streams block",
	)
	suggestSegments := flag.Int(
		"suggest-segments",
		4,
		"maximum closed segments sampled by -suggest-streams (2-128)",
	)
	flag.Parse()
	if *showVersion && *suggestStreams {
		return errors.New("-version and -suggest-streams are mutually exclusive")
	}
	if *showVersion {
		fmt.Println(version)
		return nil
	}
	if *suggestStreams {
		return runStreamSuggestion(*configPath, *suggestSegments)
	}
	cfg, err := config.Load(*configPath)
	if err != nil {
		return err
	}
	logger := log.New(os.Stderr, "comma-companion-agent: ", log.LstdFlags|log.LUTC|log.Lmsgprefix)
	runner, err := agent.New(cfg, version, logger)
	if err != nil {
		return err
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	err = runner.Run(ctx)
	if errors.Is(err, context.Canceled) {
		return nil
	}
	if errors.Is(err, agent.ErrRestartRequested) {
		logger.Printf("restart requested; exiting for systemd")
		return nil
	}
	return err
}

func runStreamSuggestion(configPath string, sampleLimit int) error {
	cfg, err := config.LoadForInspection(configPath)
	if err != nil {
		return err
	}
	if err := cfg.ValidateDevicePaths(); err != nil {
		return err
	}
	status := policy.New(cfg.Policy).Read()
	if !status.OffroadKnown || !status.Offroad || !status.OffroadSourceFresh {
		return errors.New(
			"stream discovery requires a confirmed, fresh offroad state and IsOnroad=0",
		)
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	suggestion, err := scanner.SuggestExpectedStreams(
		ctx,
		cfg,
		sampleLimit,
		status.ObservedAt,
	)
	if err != nil {
		return err
	}
	fmt.Fprintf(
		os.Stderr,
		"sampled %d closed segment(s) across %d route(s)\n",
		suggestion.SampledSegments,
		len(suggestion.SampledRoutes),
	)
	for _, coverage := range suggestion.Coverage {
		label := "observed"
		if coverage.PresentSegments != coverage.SampledSegments {
			label = "INCONSISTENT"
		}
		fmt.Fprintf(
			os.Stderr,
			"%s: %s in %d/%d sampled segments\n",
			label,
			coverage.Role,
			coverage.PresentSegments,
			coverage.SampledSegments,
		)
	}
	encoder := json.NewEncoder(os.Stdout)
	encoder.SetIndent("", "  ")
	return encoder.Encode(config.Inventory{
		ExpectedStreams: suggestion.ExpectedStreams,
	})
}

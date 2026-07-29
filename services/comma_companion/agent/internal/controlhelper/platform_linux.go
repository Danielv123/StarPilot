//go:build linux

package controlhelper

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"

	"starpilot.local/comma-companion-agent/internal/control"
)

type PeerPolicy struct {
	AllowedUID uint32
	CgroupUnit string
	Executable string
}

func RunningAsRoot() bool {
	return os.Geteuid() == 0
}

func VerifyPeer(policy PeerPolicy) PeerValidator {
	return func(connection net.Conn) error {
		unixConnection, ok := connection.(*net.UnixConn)
		if !ok {
			return errors.New("control connection is not Unix")
		}
		raw, err := unixConnection.SyscallConn()
		if err != nil {
			return err
		}
		var credential *syscall.Ucred
		var socketError error
		if err := raw.Control(func(fd uintptr) {
			credential, socketError = syscall.GetsockoptUcred(
				int(fd),
				syscall.SOL_SOCKET,
				syscall.SO_PEERCRED,
			)
		}); err != nil {
			return err
		}
		if socketError != nil {
			return socketError
		}
		if credential == nil || credential.Uid != policy.AllowedUID ||
			credential.Pid <= 0 {
			return errors.New("control peer UID is not authorized")
		}
		pid := strconv.Itoa(int(credential.Pid))
		cgroupRaw, err := os.ReadFile(filepath.Join("/proc", pid, "cgroup"))
		if err != nil || len(cgroupRaw) > 64<<10 ||
			!cgroupContainsUnit(string(cgroupRaw), policy.CgroupUnit) {
			return errors.New("control peer is outside the archive-agent cgroup")
		}
		peerExecutable, err := os.Stat(filepath.Join("/proc", pid, "exe"))
		if err != nil {
			return errors.New("cannot inspect control peer executable")
		}
		allowedExecutable, err := os.Stat(policy.Executable)
		if err != nil || !allowedExecutable.Mode().IsRegular() ||
			!os.SameFile(peerExecutable, allowedExecutable) {
			return errors.New("control peer executable is not the selected archive agent")
		}
		return nil
	}
}

func ListenUnix(socketPath string, groupID int) (net.Listener, func(), error) {
	parent := filepath.Dir(socketPath)
	if err := os.MkdirAll(parent, 0o750); err != nil {
		return nil, nil, err
	}
	parentInfo, err := os.Lstat(parent)
	if err != nil || !parentInfo.IsDir() || parentInfo.Mode()&os.ModeSymlink != 0 {
		return nil, nil, errors.New("control socket parent must be a non-symlink directory")
	}
	if err := os.Chown(parent, 0, groupID); err != nil {
		return nil, nil, err
	}
	if err := os.Chmod(parent, 0o750); err != nil {
		return nil, nil, err
	}
	if existing, err := os.Lstat(socketPath); err == nil {
		stat, ok := existing.Sys().(*syscall.Stat_t)
		if !ok || existing.Mode()&os.ModeSocket == 0 || stat.Uid != 0 {
			return nil, nil, errors.New("refusing to replace an untrusted control socket path")
		}
		if err := os.Remove(socketPath); err != nil {
			return nil, nil, err
		}
	} else if !errors.Is(err, os.ErrNotExist) {
		return nil, nil, err
	}
	listener, err := net.Listen("unix", socketPath)
	if err != nil {
		return nil, nil, err
	}
	cleanup := func() {
		_ = listener.Close()
		if info, statErr := os.Lstat(socketPath); statErr == nil &&
			info.Mode()&os.ModeSocket != 0 {
			_ = os.Remove(socketPath)
		}
	}
	if err := os.Chown(socketPath, 0, groupID); err != nil {
		cleanup()
		return nil, nil, err
	}
	if err := os.Chmod(socketPath, 0o660); err != nil {
		cleanup()
		return nil, nil, err
	}
	return listener, cleanup, nil
}

func ProductionExecutor(paramsBase string) Executor {
	return func(ctx context.Context, operation control.Operation, _ string) error {
		switch operation {
		case control.RestartStarPilot:
			command := exec.CommandContext(
				ctx,
				"/usr/bin/systemctl",
				"restart",
				"comma.service",
			)
			output, err := command.CombinedOutput()
			if err != nil {
				return fmt.Errorf(
					"systemctl restart comma.service: %w: %s",
					err,
					strings.TrimSpace(string(output)),
				)
			}
			return nil
		case control.RebootDevice:
			return putParam(paramsBase, "DoReboot", "1")
		case control.ShutdownDevice:
			return putParam(paramsBase, "DoShutdown", "1")
		default:
			return errors.New("unsupported fixed privileged operation")
		}
	}
}

func ReadRootSecret(path string) ([]byte, error) {
	info, err := os.Lstat(path)
	if err != nil {
		return nil, err
	}
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !ok || stat.Uid != 0 || !info.Mode().IsRegular() ||
		info.Mode()&os.ModeSymlink != 0 {
		return nil, errors.New("control secret must be a root-owned regular non-symlink file")
	}
	if permissions := info.Mode().Perm(); permissions != 0o400 && permissions != 0o600 {
		return nil, errors.New("control secret permissions must be 0400 or 0600")
	}
	handle, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer handle.Close()
	raw, err := io.ReadAll(io.LimitReader(handle, 4097))
	if err != nil || len(raw) < 16 || len(raw) > 4096 {
		return nil, errors.New("control secret must contain between 16 and 4096 bytes")
	}
	secret := strings.TrimSpace(string(raw))
	if len(secret) < 16 || len(secret) > 4096 {
		return nil, errors.New("control secret has an invalid length")
	}
	for _, character := range secret {
		if character <= 0x20 || character == 0x7f {
			return nil, errors.New("control secret contains whitespace or control characters")
		}
	}
	return []byte(secret), nil
}

func cgroupContainsUnit(raw, unit string) bool {
	if unit == "" || strings.Contains(unit, "/") {
		return false
	}
	for _, line := range strings.Split(raw, "\n") {
		parts := strings.SplitN(line, ":", 3)
		if len(parts) != 3 {
			continue
		}
		for _, component := range strings.Split(parts[2], "/") {
			if component == unit {
				return true
			}
		}
	}
	return false
}

func putParam(paramsBase, key, value string) error {
	if key != "DoReboot" && key != "DoShutdown" {
		return errors.New("unsupported Params key")
	}
	baseInfo, err := os.Lstat(paramsBase)
	if err != nil || !baseInfo.IsDir() || baseInfo.Mode()&os.ModeSymlink != 0 {
		return errors.New("Params base is not a non-symlink directory")
	}
	activeLink := filepath.Join(paramsBase, "d")
	activeDirectory, err := filepath.EvalSymlinks(activeLink)
	if err != nil {
		return fmt.Errorf("resolve active Params directory: %w", err)
	}
	relative, err := filepath.Rel(paramsBase, activeDirectory)
	if err != nil || relative == "." || relative == ".." ||
		strings.HasPrefix(relative, ".."+string(filepath.Separator)) {
		return errors.New("active Params directory escaped its base")
	}
	activeInfo, err := os.Stat(activeDirectory)
	if err != nil || !activeInfo.IsDir() {
		return errors.New("active Params target is not a directory")
	}
	lock, err := os.OpenFile(filepath.Join(paramsBase, ".lock"), os.O_CREATE|os.O_RDWR, 0o775)
	if err != nil {
		return err
	}
	defer lock.Close()
	if err := syscall.Flock(int(lock.Fd()), syscall.LOCK_EX); err != nil {
		return err
	}
	defer syscall.Flock(int(lock.Fd()), syscall.LOCK_UN)
	temporary, err := os.CreateTemp(paramsBase, ".tmp_control_value_*")
	if err != nil {
		return err
	}
	temporaryPath := temporary.Name()
	defer os.Remove(temporaryPath)
	if _, err := temporary.WriteString(value); err != nil {
		_ = temporary.Close()
		return err
	}
	if err := temporary.Sync(); err != nil {
		_ = temporary.Close()
		return err
	}
	if err := temporary.Close(); err != nil {
		return err
	}
	if err := os.Rename(temporaryPath, filepath.Join(activeLink, key)); err != nil {
		return err
	}
	directory, err := os.Open(activeDirectory)
	if err != nil {
		return err
	}
	defer directory.Close()
	return directory.Sync()
}

func validateRootOwnedProof(info os.FileInfo) error {
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !ok || stat.Uid != 0 {
		return errors.New("raw panda ignition proof must be root-owned")
	}
	if permissions := info.Mode().Perm(); permissions != 0o400 && permissions != 0o600 {
		return errors.New("raw panda ignition proof permissions must be 0400 or 0600")
	}
	return nil
}

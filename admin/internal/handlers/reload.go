package handlers

import (
	"fmt"
	"log"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"
)

// reloadSynapse sends SIGHUP to all Synapse processes so they reload config
// without a full restart. Returns nil only if at least one SIGHUP was delivered.
func reloadSynapse() error {
	pids, err := findSynapsePIDs()
	if err != nil {
		return fmt.Errorf("find synapse: %w", err)
	}
	if len(pids) == 0 {
		return fmt.Errorf("no synapse processes found")
	}
	signaled := 0
	for _, pid := range pids {
		proc, err := os.FindProcess(pid)
		if err != nil {
			log.Printf("reload: find process %d: %v", pid, err)
			continue
		}
		if err := proc.Signal(syscall.SIGHUP); err != nil {
			log.Printf("reload: SIGHUP pid %d: %v", pid, err)
		} else {
			log.Printf("reload: sent SIGHUP to pid %d", pid)
			signaled++
		}
	}
	if signaled == 0 {
		return fmt.Errorf("failed to signal any of %d synapse process(es)", len(pids))
	}
	// Brief pause to let Synapse start reloading before we respond
	time.Sleep(500 * time.Millisecond)
	return nil
}

func findSynapsePIDs() ([]int, error) {
	entries, err := os.ReadDir("/proc")
	if err != nil {
		return nil, err
	}
	var pids []int
	for _, e := range entries {
		if !e.IsDir() {
			continue
		}
		if _, err := strconv.Atoi(e.Name()); err != nil {
			continue
		}
		cmdlineFile := filepath.Join("/proc", e.Name(), "cmdline")
		raw, err := os.ReadFile(cmdlineFile)
		if err != nil {
			continue
		}
		cmdline := string(raw)
		// Replace null bytes with spaces for matching
		cmdline = strings.ReplaceAll(cmdline, "\x00", " ")
		if strings.Contains(cmdline, "synapse") && strings.Contains(cmdline, "python") {
			pid, _ := strconv.Atoi(e.Name())
			pids = append(pids, pid)
		}
	}
	return pids, nil
}

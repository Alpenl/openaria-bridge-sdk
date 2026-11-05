package main

import (
	"context"
	"encoding/json"
	"fmt"
	"openaria-exporter/internal/source"
	"os/exec"
	"strings"
)

// Query physical external disks only; mounted DMGs and internal disks never
// appear as card candidates. The privileged opener independently rechecks them.
func externalPartitions(ctx context.Context) (cardInventory, error) {
	b, e := exec.CommandContext(ctx, "/usr/sbin/diskutil", "list", "-plist", "external", "physical").Output()
	if e != nil {
		return cardInventory{}, fmt.Errorf("读取外置设备列表：%w", e)
	}
	cmd := exec.CommandContext(ctx, "/usr/bin/plutil", "-convert", "json", "-o", "-", "--", "-")
	cmd.Stdin = strings.NewReader(string(b))
	b, e = cmd.Output()
	if e != nil {
		return cardInventory{}, e
	}
	return parseExternalDisks(b)
}
func parseExternalDisks(b []byte) (cardInventory, error) {
	type part struct {
		DeviceIdentifier, Content string
		Size                      uint64
	}
	var root struct {
		AllDisksAndPartitions []struct {
			DeviceIdentifier, Content string
			Size                      uint64
			Partitions                []part
		}
	}
	if e := json.Unmarshal(b, &root); e != nil {
		return cardInventory{}, e
	}
	out := cardInventory{External: len(root.AllDisksAndPartitions)}
	for _, disk := range root.AllDisksAndPartitions {
		for _, p := range disk.Partitions {
			if strings.Contains(strings.ToLower(p.Content), "linux") {
				out.Candidates = append(out.Candidates, Device{"/dev/" + p.DeviceIdentifier, fmt.Sprintf("%.1f GiB", float64(disk.Size)/(1<<30))})
			}
		}
	}
	return out, nil
}
func passiveCardSource(ctx context.Context, p string) (source.Source, error) {
	return authorizedSource(ctx, p)
}

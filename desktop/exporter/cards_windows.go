package main

import (
	"context"
	"encoding/json"
	"fmt"
	"openaria-exporter/internal/process"
	"openaria-exporter/internal/source"
)

func externalPartitions(ctx context.Context) (cardInventory, error) {
	b, e := process.CommandContext(ctx, "powershell.exe", "-NoProfile", "-NonInteractive", "-Command", `$ErrorActionPreference='Stop'; $ds=@(Get-Disk | Where-Object {$_.BusType -in @('USB','SD','MMC')}); @{Count=$ds.Count; Partitions=@($ds | ForEach-Object {$d=$_; Get-Partition -DiskNumber $d.Number | ForEach-Object {@{Disk=$d.Number;Partition=$_.PartitionNumber;Offset=$_.Offset;Size=$_.Size;Name=$d.FriendlyName}}})} | ConvertTo-Json -Compress -Depth 4`).Output()
	if e != nil {
		return cardInventory{}, e
	}
	var result struct {
		Count      int
		Partitions json.RawMessage
	}
	if e = json.Unmarshal(b, &result); e != nil {
		return cardInventory{}, e
	}
	ps, e := parseWinPartitions(result.Partitions)
	if e != nil {
		return cardInventory{}, e
	}
	out := cardInventory{External: result.Count}
	for _, p := range ps {
		out.Candidates = append(out.Candidates, Device{winPath(p), fmt.Sprintf("%s · %.1f GiB", p.Name, float64(p.Size)/(1<<30))})
	}
	return out, nil
}
func passiveCardSource(ctx context.Context, p string) (source.Source, error) {
	return authorizedSource(ctx, p)
}

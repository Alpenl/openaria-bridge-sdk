package main

import (
	"context"
	"fmt"
	"io"
	"openaria-exporter/internal/process"
	"openaria-exporter/internal/source"
	"os"
	"os/exec"
	"syscall"
	"time"
)

func winParts() ([]winPartition, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 8*time.Second)
	defer cancel()
	b, e := process.CommandContext(ctx, "powershell.exe", "-NoProfile", "-NonInteractive", "-Command", `$ErrorActionPreference='Stop'; @(Get-Disk | Where-Object {$_.BusType -in @('USB','SD','MMC')} | ForEach-Object { $d=$_; Get-Partition -DiskNumber $d.Number | ForEach-Object { @{Disk=$d.Number;Partition=$_.PartitionNumber;Offset=$_.Offset;Size=$_.Size;Name=$d.FriendlyName} } }) | ConvertTo-Json -Compress`).Output()
	if e != nil {
		if x, ok := e.(*exec.ExitError); ok {
			return nil, fmt.Errorf("enumerate: %w: %s", e, x.Stderr)
		}
		return nil, e
	}
	return parseWinPartitions(b)
}
func winPath(p winPartition) string { return fmt.Sprintf("usb-disk:%d:%d", p.Disk, p.Partition) }
func devices() ([]Device, error) {
	ps, e := winParts()
	var ds []Device
	for _, p := range ps {
		ds = append(ds, Device{winPath(p), fmt.Sprintf("%s 分区%d %.1f GiB", p.Name, p.Partition, float64(p.Size)/(1<<30))})
	}
	return ds, e
}
func selectedWin(p string) (winPartition, error) {
	ps, e := winParts()
	if e != nil {
		return winPartition{}, e
	}
	for _, v := range ps {
		if winPath(v) == p && v.Offset >= 0 && v.Size > 0 && v.Offset <= int64(^uint64(0)>>1)-v.Size {
			return v, nil
		}
	}
	return winPartition{}, fmt.Errorf("USB 分区已改变或不存在")
}
func openWin(p winPartition) (*os.File, error) {
	name, _ := syscall.UTF16PtrFromString(fmt.Sprintf(`\\.\PhysicalDrive%d`, p.Disk))
	h, e := syscall.CreateFile(name, syscall.GENERIC_READ, syscall.FILE_SHARE_READ|syscall.FILE_SHARE_WRITE, nil, syscall.OPEN_EXISTING, syscall.FILE_ATTRIBUTE_NORMAL, 0)
	if e != nil {
		return nil, e
	}
	return os.NewFile(uintptr(h), "readonly USB"), nil
}
func winSource(f *os.File, p winPartition) (source.Source, error) {
	return source.FromReader(io.NewSectionReader(source.AlignedReader{Reader: f, Sector: 4096}, p.Offset, p.Size), f.Close)
}
func authorizedSource(ctx context.Context, p string) (source.Source, error) {
	part, e := selectedWin(p)
	if e != nil {
		return nil, e
	}
	f, e := openWin(part)
	if e != nil {
		return nil, e
	}
	return winSource(f, part)
}
func helper([]string) error {
	return fmt.Errorf("旧 helper 入口已移除，请重新启动应用并允许 UAC")
}

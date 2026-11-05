package main

import (
	"context"
	"fmt"
	"net"
	"openaria-exporter/internal/privileged"
	"openaria-exporter/internal/source"
	"path/filepath"
)

type DiskInfo = privileged.DiskInfo

func parseDiskInfo(b []byte) (DiskInfo, error) { return privileged.ParseDiskInfo(b) }
func eligible(d DiskInfo) bool                 { return privileged.Eligible(d) }
func diskInfo(p string) (DiskInfo, error)      { return privileged.Disk(p) }
func peerUID(c *net.UnixConn) (uint32, error)  { return privileged.PeerUID(c) }
func devices() ([]Device, error) {
	ps, e := filepath.Glob("/dev/disk*s*")
	if e != nil {
		return nil, e
	}
	return enumeratePartitions(ps, diskInfo)
}
func enumeratePartitions(ps []string, info func(string) (DiskInfo, error)) ([]Device, error) {
	var out []Device
	for _, p := range ps {
		if !privileged.CandidatePath(p) {
			continue
		}
		d, e := info(p)
		if e != nil {
			return out, e
		}
		if eligible(d) {
			out = append(out, Device{p, fmt.Sprintf("%s %.1f GiB", d.MediaName, float64(d.TotalSize)/(1<<30))})
		}
	}
	return out, nil
}
func authorizedSource(ctx context.Context, p string) (s source.Source, err error) {
	defer func() { err = authorizationError(ctx, err) }()
	f, e := privileged.RequestFile(ctx, "open", p)
	if e != nil {
		return nil, e
	}
	return source.FromFile(f)
}
func helper([]string) error {
	return fmt.Errorf("旧提权入口已移除；请安装 PKG 以配置独立只读服务")
}

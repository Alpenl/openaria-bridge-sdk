package main

import (
	"errors"
	"net"
	"os"
	"path/filepath"
	"syscall"
	"testing"
)

func TestPeerIdentity(t *testing.T) {
	p := filepath.Join(t.TempDir(), "s")
	l, e := net.ListenUnix("unix", &net.UnixAddr{Name: p, Net: "unix"})
	if e != nil {
		if errors.Is(e, syscall.EPERM) {
			t.Skip("环境禁止 Unix socket bind：peer UID 实测待验收")
		}
		t.Fatal(e)
	}
	defer l.Close()
	done := make(chan error, 1)
	go func() {
		c, e := net.DialUnix("unix", nil, &net.UnixAddr{Name: p, Net: "unix"})
		if e == nil {
			defer c.Close()
			_, e = c.Write([]byte("x"))
		}
		done <- e
	}()
	c, e := l.AcceptUnix()
	if e != nil {
		t.Fatal(e)
	}
	defer c.Close()
	uid, e := peerUID(c)
	if e != nil || uid != uint32(os.Getuid()) {
		t.Fatalf("uid %d error %v", uid, e)
	}
	if e = <-done; e != nil {
		t.Fatal(e)
	}
}

func TestDiskutilRealFields(t *testing.T) {
	d, e := parseDiskInfo([]byte(`{"WholeDisk":false,"Internal":false,"Content":"Linux","TotalSize":50000000000,"MediaName":"USB"}`))
	if e != nil || !eligible(d) {
		t.Fatalf("%+v %v", d, e)
	}
	d, e = parseDiskInfo([]byte(`{"Whole":false,"Internal":false,"Content":"Linux"}`))
	if e != nil || eligible(d) {
		t.Fatal("missing WholeDisk accepted")
	}
}

func TestEnumerationSkipsAPFSSnapshot(t *testing.T) {
	calls := 0
	out, e := enumeratePartitions([]string{"/dev/disk3s1s1", "/dev/disk8s2"}, func(p string) (DiskInfo, error) {
		calls++
		if p != "/dev/disk8s2" {
			t.Fatalf("unexpected candidate %s", p)
		}
		return parseDiskInfo([]byte(`{"Internal":false,"WholeDisk":false,"Content":"Linux"}`))
	})
	if e != nil || calls != 1 || len(out) != 1 {
		t.Fatalf("%v %d %v", e, calls, out)
	}
}

package privileged

import (
	"context"
	"encoding/json"
	"errors"
	"net"
	"os"
	"syscall"
	"testing"
	"time"
)

func TestProtocol(t *testing.T) {
	for _, r := range []Request{{2, "x", "status", ""}, {1, "x", "open", "/etc/passwd"}, {1, "x", "open", "/dev/disk3s1s1"}, {1, "x", "write", "/dev/disk8s2"}, {1, "x", "status", "/dev/disk8s2"}} {
		if validate(r) == nil {
			t.Fatal(r)
		}
	}
	if validate(Request{1, "x", "open", "/dev/disk8s2"}) != nil {
		t.Fatal("valid request")
	}
	for _, s := range []string{`{"Internal":true,"WholeDisk":false,"Content":"Linux"}`, `{"Internal":false,"WholeDisk":true,"Content":"Linux"}`} {
		d, _ := ParseDiskInfo([]byte(s))
		if Eligible(d) {
			t.Fatal(s)
		}
	}
}
func pair(t *testing.T) (*net.UnixConn, *net.UnixConn) {
	t.Helper()
	fds, e := syscall.Socketpair(syscall.AF_UNIX, syscall.SOCK_STREAM, 0)
	if e != nil {
		t.Fatal(e)
	}
	conns := make([]*net.UnixConn, 2)
	for i, fd := range fds {
		f := os.NewFile(uintptr(fd), "socketpair")
		c, e := net.FileConn(f)
		f.Close()
		if e != nil {
			t.Fatal(e)
		}
		conns[i] = c.(*net.UnixConn)
	}
	c, s := conns[0], conns[1]
	t.Cleanup(func() { c.Close(); s.Close() })
	c.SetDeadline(time.Now().Add(time.Second))
	return c, s
}
func TestUIDAndFD(t *testing.T) {
	for _, reject := range []bool{false, true} {
		t.Run(map[bool]string{false: "readonly", true: "UID-reject"}[reject], func(t *testing.T) {
			c, s := pair(t)
			uid := uint32(os.Getuid())
			if reject {
				uid++
			}
			done := make(chan struct{})
			var sent *os.File
			go func() {
				defer close(done)
				handle(s, uid, func(string) (*os.File, error) {
					var e error
					sent, e = os.Open("service_darwin_test.go")
					return sent, e
				})
			}()
			b, _ := json.Marshal(Request{1, "test", "open", "/dev/disk8s2"})
			c.Write(append(b, '\n'))
			buf := make([]byte, 1024)
			oob := make([]byte, 128)
			_, n, _, _, e := c.ReadMsgUnix(buf, oob)
			<-done
			if reject {
				if e == nil {
					t.Fatal("unauthorized accepted")
				}
				return
			}
			if e != nil {
				t.Fatal(e)
			}
			fds, e := rightsFDs(oob[:n])
			if e != nil || len(fds) != 1 {
				t.Fatalf("fds %v %v", fds, e)
			}
			defer syscall.Close(fds[0])
			if e = ReadOnlyFD(fds[0], false); e != nil {
				t.Fatal(e)
			}
			if _, e = sent.Stat(); e == nil {
				t.Fatal("sender fd leaked")
			}
		})
	}
}
func TestWritableFDRejected(t *testing.T) {
	f, e := os.CreateTemp(t.TempDir(), "rw")
	if e != nil {
		t.Fatal(e)
	}
	defer f.Close()
	if ReadOnlyFD(int(f.Fd()), false) == nil {
		t.Fatal("writable accepted")
	}
}
func TestDisconnectCleanup(t *testing.T) {
	c, s := pair(t)
	done := make(chan struct{})
	go func() {
		handle(s, uint32(os.Getuid()), func(string) (*os.File, error) { t.Error("open after disconnect"); return nil, nil })
		close(done)
	}()
	c.Write([]byte(`{"version":`))
	c.Close()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("disconnect leaked handler")
	}
}

func TestCanceledRequest(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	f, e := RequestFile(ctx, "open", "/dev/disk8s2")
	if f != nil || e == nil || !errors.Is(e, context.Canceled) {
		t.Fatalf("%v %v", f, e)
	}
}

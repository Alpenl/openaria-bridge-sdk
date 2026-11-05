// Package privileged implements the versioned, read-only macOS device service.
package privileged

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net"
	"os"
	"os/exec"
	"regexp"
	"strconv"
	"strings"
	"syscall"
	"time"
	"unsafe"
)

const Version = 1
const Socket = "/var/run/dev.alpen.openaria-exporter.sock"
const Config = "/Library/PrivilegedHelperTools/dev.alpen.openaria-exporter.uid"
const Helper = "/Library/PrivilegedHelperTools/dev.alpen.openaria-exporter.helper"
const Label = "dev.alpen.openaria-exporter"
const MaxMessage = 1024

var pathPattern = regexp.MustCompile(`^/dev/disk[0-9]+s[0-9]+$`)

type DiskInfo struct {
	Internal, WholeDisk                                          *bool
	Content, MediaName, VolumeName, DeviceIdentifier, DeviceNode string
	TotalSize                                                    uint64
}

func ParseDiskInfo(b []byte) (d DiskInfo, err error) { err = json.Unmarshal(b, &d); return }
func Eligible(d DiskInfo) bool {
	return d.Internal != nil && !*d.Internal && d.WholeDisk != nil && !*d.WholeDisk && strings.Contains(strings.ToLower(d.Content), "linux")
}
func CandidatePath(p string) bool { return pathPattern.MatchString(p) }
func Disk(p string) (DiskInfo, error) {
	if !pathPattern.MatchString(p) {
		return DiskInfo{}, fmt.Errorf("invalid device path")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, "/usr/sbin/diskutil", "info", "-plist", p)
	b, e := cmd.Output()
	if e != nil {
		return DiskInfo{}, commandError("diskutil", e)
	}
	cmd = exec.CommandContext(ctx, "/usr/bin/plutil", "-convert", "json", "-o", "-", "--", "-")
	cmd.Stdin = strings.NewReader(string(b))
	b, e = cmd.Output()
	if e != nil {
		return DiskInfo{}, commandError("plutil", e)
	}
	return ParseDiskInfo(b)
}
func commandError(stage string, e error) error {
	if x, ok := e.(*exec.ExitError); ok {
		return fmt.Errorf("%s: %w: %.512s", stage, e, x.Stderr)
	}
	return fmt.Errorf("%s: %w", stage, e)
}
func PeerUID(c *net.UnixConn) (uint32, error) {
	raw, e := c.SyscallConn()
	if e != nil {
		return 0, e
	}
	var cred struct {
		Version, UID uint32
		NGroups      int16
		Groups       [16]uint32
	}
	size := uint32(unsafe.Sizeof(cred))
	var errno syscall.Errno
	e = raw.Control(func(fd uintptr) {
		_, _, errno = syscall.Syscall6(syscall.SYS_GETSOCKOPT, fd, 0, 1, uintptr(unsafe.Pointer(&cred)), uintptr(unsafe.Pointer(&size)), 0)
	})
	if e != nil {
		return 0, e
	}
	if errno != 0 {
		return 0, errno
	}
	if cred.Version != 0 || size < 12 {
		return 0, fmt.Errorf("invalid peer credentials")
	}
	return cred.UID, nil
}
func ReadUID() (uint32, error) {
	fd, e := syscall.Open(Config, syscall.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_CLOEXEC, 0)
	if e != nil {
		return 0, e
	}
	f := os.NewFile(uintptr(fd), Config)
	defer f.Close()
	var s syscall.Stat_t
	if e = syscall.Fstat(fd, &s); e != nil {
		return 0, e
	}
	if s.Uid != 0 || s.Mode&0022 != 0 || s.Mode&syscall.S_IFMT != syscall.S_IFREG {
		return 0, fmt.Errorf("insecure UID configuration")
	}
	b, e := io.ReadAll(io.LimitReader(f, 32))
	if e != nil {
		return 0, e
	}
	u, e := strconv.ParseUint(strings.TrimSpace(string(b)), 10, 32)
	if e != nil || u == 0 || u == 4294967295 {
		return 0, fmt.Errorf("invalid authorized UID")
	}
	return uint32(u), nil
}

type Request struct {
	Version int    `json:"version"`
	ID      string `json:"id"`
	Op      string `json:"op"`
	Path    string `json:"path,omitempty"`
}
type Response struct {
	Version int    `json:"version"`
	ID      string `json:"id"`
	Error   string `json:"error,omitempty"`
}

func validate(r Request) error {
	if r.Version != Version {
		return fmt.Errorf("incompatible protocol; install current PKG")
	}
	if !regexp.MustCompile(`^[a-zA-Z0-9-]{1,64}$`).MatchString(r.ID) {
		return fmt.Errorf("invalid request ID")
	}
	if r.Op == "status" && r.Path == "" {
		return nil
	}
	if r.Op != "open" || !pathPattern.MatchString(r.Path) {
		return fmt.Errorf("invalid operation/path")
	}
	return nil
}
func OpenDevice(p string) (*os.File, error) {
	if !pathPattern.MatchString(p) {
		return nil, fmt.Errorf("invalid device path")
	}
	d, e := Disk(p)
	if e != nil {
		return nil, e
	}
	if !Eligible(d) || d.DeviceNode != p || d.DeviceIdentifier != strings.TrimPrefix(p, "/dev/") {
		return nil, fmt.Errorf("not an external Linux partition")
	}
	var before syscall.Stat_t
	if e = syscall.Lstat(p, &before); e != nil {
		return nil, e
	}
	if before.Mode&syscall.S_IFMT != syscall.S_IFBLK {
		return nil, fmt.Errorf("not a block device")
	}
	fd, e := syscall.Open(p, syscall.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_CLOEXEC, 0)
	if e != nil {
		return nil, e
	}
	f := os.NewFile(uintptr(fd), p)
	var after syscall.Stat_t
	e = syscall.Fstat(fd, &after)
	if e != nil || before.Rdev != after.Rdev || before.Dev != after.Dev || before.Ino != after.Ino || after.Mode&syscall.S_IFMT != syscall.S_IFBLK {
		f.Close()
		return nil, fmt.Errorf("device identity changed")
	}
	// Re-query after open as well: refuse a changed/reclassified device.
	d2, e := Disk(p)
	if e != nil || !Eligible(d2) || d2.DeviceNode != d.DeviceNode || d2.DeviceIdentifier != d.DeviceIdentifier || d2.TotalSize != d.TotalSize {
		f.Close()
		return nil, fmt.Errorf("device metadata changed")
	}
	return f, nil
}
func Serve(l *net.UnixListener, uid uint32) error {
	slots := make(chan struct{}, 8)
	for {
		c, e := l.AcceptUnix()
		if e != nil {
			return e
		}
		select {
		case slots <- struct{}{}:
			go func() { defer func() { <-slots }(); handle(c, uid, OpenDevice) }()
		default:
			c.Close()
		}
	}
}
func handle(c *net.UnixConn, uid uint32, open func(string) (*os.File, error)) {
	defer c.Close()
	c.SetDeadline(time.Now().Add(15 * time.Second))
	peer, e := PeerUID(c)
	if e != nil || peer != uid || uid == 0 {
		log.Printf("stage=peer rejected uid=%d", peer)
		return
	}
	// Stream framing: exactly one newline-terminated bounded request per connection.
	b := make([]byte, 0, MaxMessage)
	one := make([]byte, 1)
	for len(b) < MaxMessage {
		if _, e = io.ReadFull(c, one); e != nil {
			return
		}
		if one[0] == '\n' {
			break
		}
		b = append(b, one[0])
	}
	var r Request
	dec := json.NewDecoder(strings.NewReader(string(b)))
	dec.DisallowUnknownFields()
	e = dec.Decode(&r)
	if e == nil {
		var extra any
		if dec.Decode(&extra) != io.EOF {
			e = fmt.Errorf("trailing request")
		}
	}
	if len(b) >= MaxMessage {
		e = fmt.Errorf("message limit")
	}
	if e == nil {
		e = validate(r)
	}
	var f *os.File
	if e == nil && r.Op == "open" {
		f, e = open(r.Path)
		if f != nil {
			defer f.Close()
		}
	}
	resp := Response{Version: Version, ID: r.ID}
	if e != nil {
		resp.Error = e.Error()
	}
	data, _ := json.Marshal(resp)
	var rights []byte
	if f != nil && e == nil {
		rights = syscall.UnixRights(int(f.Fd()))
	}
	_, _, sendErr := c.WriteMsgUnix(append(data, '\n'), rights, nil)
	log.Printf("request=%q stage=%s result=%v send=%v", r.ID, r.Op, e, sendErr)
}
func RequestFile(ctx context.Context, op, p string) (*os.File, error) {
	d := net.Dialer{}
	conn, e := d.DialContext(ctx, "unix", Socket)
	if e != nil {
		return nil, fmt.Errorf("服务未安装或不可用，请通过系统 Installer 安装 Open Aria Exporter PKG: %w", e)
	}
	c := conn.(*net.UnixConn)
	defer c.Close()
	c.SetDeadline(time.Now().Add(15 * time.Second))
	stop := context.AfterFunc(ctx, func() { c.Close() })
	defer stop()
	uid, e := PeerUID(c)
	if e != nil || uid != 0 {
		return nil, fmt.Errorf("service is not root")
	}
	id := strconv.FormatInt(time.Now().UnixNano(), 36)
	r := Request{Version: Version, ID: id, Op: op, Path: p}
	b, _ := json.Marshal(r)
	if _, e = c.Write(append(b, '\n')); e != nil {
		return nil, e
	}
	data := make([]byte, MaxMessage)
	control := make([]byte, syscall.CmsgSpace(4*8))
	n, o, flags, _, e := c.ReadMsgUnix(data, control)
	fds, ce := rightsFDs(control[:o])
	defer func() {
		for _, fd := range fds {
			syscall.Close(fd)
		}
	}()
	if e != nil {
		return nil, e
	}
	if ce != nil {
		return nil, ce
	}
	if flags&(syscall.MSG_CTRUNC|syscall.MSG_TRUNC) != 0 {
		return nil, fmt.Errorf("truncated response")
	}
	// Unix streams may split response data; rights arrive with the first byte.
	for n < MaxMessage && (n == 0 || data[n-1] != '\n') {
		k, er := c.Read(data[n:])
		n += k
		if er != nil {
			return nil, er
		}
	}
	var resp Response
	if e = json.Unmarshal(data[:n], &resp); e != nil {
		return nil, e
	}
	if resp.Version != Version || resp.ID != id {
		return nil, fmt.Errorf("服务协议不兼容，请安装新版 PKG")
	}
	if resp.Error != "" {
		return nil, fmt.Errorf("request=%s: %s", id, resp.Error)
	}
	if op == "status" {
		if len(fds) != 0 {
			return nil, fmt.Errorf("unexpected fd")
		}
		return nil, nil
	}
	if len(fds) != 1 {
		return nil, fmt.Errorf("expected one fd")
	}
	if e = ReadOnlyFD(fds[0], true); e != nil {
		return nil, e
	}
	var st, expected syscall.Stat_t
	if syscall.Fstat(fds[0], &st) != nil || syscall.Lstat(p, &expected) != nil || st.Rdev != expected.Rdev || st.Dev != expected.Dev || st.Ino != expected.Ino {
		return nil, fmt.Errorf("received device identity mismatch")
	}
	fd := fds[0]
	syscall.CloseOnExec(fd)
	fds = nil
	return os.NewFile(uintptr(fd), p), nil
}
func rightsFDs(b []byte) ([]int, error) {
	ms, e := syscall.ParseSocketControlMessage(b)
	if e != nil {
		return nil, e
	}
	var fds []int
	for _, m := range ms {
		v, e := syscall.ParseUnixRights(&m)
		if e != nil {
			return fds, e
		}
		fds = append(fds, v...)
	}
	return fds, nil
}
func ReadOnlyFD(fd int, device bool) error {
	f, _, e := syscall.Syscall(syscall.SYS_FCNTL, uintptr(fd), syscall.F_GETFL, 0)
	if e != 0 || f&syscall.O_ACCMODE != syscall.O_RDONLY {
		return fmt.Errorf("writable fd rejected")
	}
	var s syscall.Stat_t
	if e := syscall.Fstat(fd, &s); e != nil {
		return e
	}
	if device && s.Mode&syscall.S_IFMT != syscall.S_IFBLK {
		return fmt.Errorf("non-device fd rejected")
	}
	return nil
}

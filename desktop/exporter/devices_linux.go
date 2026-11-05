package main

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net"
	"openaria-exporter/internal/source"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"syscall"
	"time"
)

func devices() ([]Device, error) {
	b, e := exec.Command("lsblk", "-J", "-p", "-o", "NAME,TYPE,TRAN,RM,FSTYPE").Output()
	if e != nil {
		return nil, e
	}
	type node struct {
		Name, Type, Tran, Fstype string
		RM                       bool
		Children                 []json.RawMessage
	}
	var root struct{ Blockdevices []json.RawMessage }
	if e = json.Unmarshal(b, &root); e != nil {
		return nil, e
	}
	var out []Device
	var walk func(json.RawMessage, bool)
	walk = func(b json.RawMessage, external bool) {
		var n node
		if json.Unmarshal(b, &n) != nil {
			return
		}
		external = external || n.RM || n.Tran == "usb"
		if external && n.Type == "part" && n.Fstype == "ext4" {
			out = append(out, Device{n.Name, "外置 ext4"})
		}
		for _, v := range n.Children {
			walk(v, external)
		}
	}
	for _, v := range root.Blockdevices {
		walk(v, false)
	}
	return out, nil
}
func authorizedSource(ctx context.Context, p string) (source.Source, error) {
	ds, e := devices()
	if e != nil {
		return nil, e
	}
	for _, d := range ds {
		if d.Path == p {
			if s, e := passiveCardSource(ctx, p); e == nil {
				return s, nil
			}
			return linuxAuthorize(ctx, p)
		}
	}
	return nil, fmt.Errorf("只允许枚举出的外置 ext4 分区；需要当前用户具有只读访问权限")
}
func uid(c *net.UnixConn) (uint32, error) {
	raw, e := c.SyscallConn()
	if e != nil {
		return 0, e
	}
	var u *syscall.Ucred
	var err error
	raw.Control(func(fd uintptr) { u, err = syscall.GetsockoptUcred(int(fd), syscall.SOL_SOCKET, syscall.SO_PEERCRED) })
	if err != nil {
		return 0, err
	}
	return u.Uid, nil
}
func linuxAuthorize(ctx context.Context, p string) (source.Source, error) {
	dir, e := os.MkdirTemp("", "aria-fd-")
	if e != nil {
		return nil, e
	}
	defer os.RemoveAll(dir)
	sock := filepath.Join(dir, "s")
	l, e := net.ListenUnix("unix", &net.UnixAddr{Name: sock, Net: "unix"})
	if e != nil {
		return nil, e
	}
	defer l.Close()
	l.SetDeadline(time.Now().Add(2 * time.Minute))
	stop := context.AfterFunc(ctx, func() { l.Close() })
	defer stop()
	b := make([]byte, 32)
	if _, e = rand.Read(b); e != nil {
		return nil, e
	}
	nonce := hex.EncodeToString(b)
	exe, _ := os.Executable()
	cmd := exec.CommandContext(ctx, "pkexec", exe, "source-helper", p, sock, nonce, strconv.Itoa(os.Getuid()))
	if e = cmd.Start(); e != nil {
		return nil, e
	}
	go func() {
		if cmd.Wait() != nil {
			l.Close()
		}
	}()
	c, e := l.AcceptUnix()
	if e != nil {
		return nil, e
	}
	defer c.Close()
	c.SetDeadline(time.Now().Add(10 * time.Second))
	u, e := uid(c)
	if e != nil || u != 0 {
		return nil, fmt.Errorf("helper UID mismatch")
	}
	data := make([]byte, 128)
	control := make([]byte, syscall.CmsgSpace(4))
	n, o, _, _, e := c.ReadMsgUnix(data, control)
	if e != nil {
		return nil, e
	}
	msgs, e := syscall.ParseSocketControlMessage(control[:o])
	if e != nil {
		return nil, e
	}
	fds := []int{}
	for _, m := range msgs {
		v, e := syscall.ParseUnixRights(&m)
		if e != nil {
			return nil, e
		}
		fds = append(fds, v...)
	}
	if string(data[:n]) != nonce || len(fds) != 1 {
		for _, fd := range fds {
			syscall.Close(fd)
		}
		return nil, fmt.Errorf("invalid helper response")
	}
	fd := fds[0]
	syscall.CloseOnExec(fd)
	flags, _, errno := syscall.Syscall(syscall.SYS_FCNTL, uintptr(fd), syscall.F_GETFL, 0)
	if errno != 0 || flags&syscall.O_ACCMODE != syscall.O_RDONLY {
		syscall.Close(fd)
		return nil, fmt.Errorf("not readonly")
	}
	f := os.NewFile(uintptr(fd), p)
	if e := rememberLinuxCard(p, f); e != nil {
		f.Close()
		return nil, e
	}
	s, e := source.FromFile(f)
	if e != nil {
		forgetLinuxCard(p)
	}
	return s, e
}
func helper(args []string) error {
	if len(args) != 4 || os.Geteuid() != 0 {
		return fmt.Errorf("invalid helper")
	}
	want, e := strconv.Atoi(args[3])
	if e != nil || want <= 0 || len(args[2]) != 64 {
		return fmt.Errorf("invalid identity")
	}
	ds, e := devices()
	if e != nil {
		return e
	}
	found := false
	for _, d := range ds {
		found = found || d.Path == args[0]
	}
	if !found {
		return fmt.Errorf("USB partition no longer present")
	}
	c, e := net.DialUnix("unix", nil, &net.UnixAddr{Name: args[1], Net: "unix"})
	if e != nil {
		return e
	}
	defer c.Close()
	c.SetDeadline(time.Now().Add(10 * time.Second))
	u, e := uid(c)
	if e != nil || u != uint32(want) {
		return fmt.Errorf("GUI UID mismatch")
	}
	f, e := os.Open(args[0])
	if e != nil {
		return e
	}
	defer f.Close()
	_, _, e = c.WriteMsgUnix([]byte(args[2]), syscall.UnixRights(int(f.Fd())), nil)
	return e
}

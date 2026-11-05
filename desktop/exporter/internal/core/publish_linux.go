package core

import (
	"fmt"
	"os"
	"runtime"
	"syscall"
	"unsafe"
)

func publish(root *os.Root, old, new string) error {
	if runtime.GOARCH != "amd64" {
		return fmt.Errorf("此 Linux 架构未实现原子不覆盖发布")
	}
	d, e := root.Open(".")
	if e != nil {
		return e
	}
	defer d.Close()
	a, e := syscall.BytePtrFromString(old)
	if e != nil {
		return e
	}
	b, e := syscall.BytePtrFromString(new)
	if e != nil {
		return e
	}
	_, _, errno := syscall.Syscall6(316, d.Fd(), uintptr(unsafe.Pointer(a)), d.Fd(), uintptr(unsafe.Pointer(b)), 1, 0)
	if errno != 0 {
		return errno
	}
	return nil
}
func available(p string) (uint64, error) {
	var s syscall.Statfs_t
	e := syscall.Statfs(p, &s)
	return s.Bavail * uint64(s.Bsize), e
}

package core

import (
	"os"
	"syscall"
	"unsafe"
)

func publish(root *os.Root, old, new string) error {
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
	_, _, errno := syscall.Syscall6(488, d.Fd(), uintptr(unsafe.Pointer(a)), d.Fd(), uintptr(unsafe.Pointer(b)), 4, 0)
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

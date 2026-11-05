package core

import (
	"os"
	"path/filepath"
	"syscall"
	"unsafe"
)

func publish(root *os.Root, old, new string) error {
	a, e := syscall.UTF16PtrFromString(filepath.Join(root.Name(), old))
	if e != nil {
		return e
	}
	b, e := syscall.UTF16PtrFromString(filepath.Join(root.Name(), new))
	if e != nil {
		return e
	}
	// MoveFile deliberately does not replace an existing destination.
	return syscall.MoveFile(a, b)
}
func available(p string) (uint64, error) {
	ptr, e := syscall.UTF16PtrFromString(p)
	if e != nil {
		return 0, e
	}
	var free, total, totalFree uint64
	proc := syscall.NewLazyDLL("kernel32.dll").NewProc("GetDiskFreeSpaceExW")
	r, _, e := proc.Call(uintptr(unsafe.Pointer(ptr)), uintptr(unsafe.Pointer(&free)), uintptr(unsafe.Pointer(&total)), uintptr(unsafe.Pointer(&totalFree)))
	if r == 0 {
		return 0, e
	}
	return free, nil
}

package core

import (
	"os"
	"syscall"
	"unsafe"
)

func lockFile(f *os.File) error {
	var ov syscall.Overlapped
	p := syscall.NewLazyDLL("kernel32.dll").NewProc("LockFileEx")
	r, _, e := p.Call(f.Fd(), 3, 0, 1, 0, uintptr(unsafe.Pointer(&ov)))
	if r == 0 {
		return e
	}
	return nil
}

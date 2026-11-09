package core

import (
	"errors"
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
func lockBusy(err error) bool { return errors.Is(err, syscall.Errno(33)) }
func hideControlDirectory(name string) error {
	p, err := syscall.UTF16PtrFromString(name)
	if err != nil {
		return err
	}
	attr, err := syscall.GetFileAttributes(p)
	if err != nil {
		return err
	}
	return syscall.SetFileAttributes(p, attr|syscall.FILE_ATTRIBUTE_HIDDEN)
}

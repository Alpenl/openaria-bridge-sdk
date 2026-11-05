package main

import (
	"fmt"
	"openaria-exporter/internal/source"
	"os"
	"sync"
	"syscall"
)

// Keep only read-only descriptors obtained by explicit polkit authorization.
// Every probe uses a duplicate with a fresh filesystem reader, so no ext4 caches
// are shared with an export and background detection never asks for a password.
var linuxCardAccess = struct {
	sync.Mutex
	files map[string]*os.File
}{files: make(map[string]*os.File)}

func rememberLinuxCard(p string, f *os.File) error {
	linuxCardAccess.Lock()
	defer linuxCardAccess.Unlock()
	fd, e := syscall.Dup(int(f.Fd()))
	if e != nil {
		return e
	}
	syscall.CloseOnExec(fd)
	if old := linuxCardAccess.files[p]; old != nil {
		old.Close()
	}
	linuxCardAccess.files[p] = os.NewFile(uintptr(fd), p)
	return nil
}
func forgetLinuxCard(p string) {
	linuxCardAccess.Lock()
	defer linuxCardAccess.Unlock()
	if f := linuxCardAccess.files[p]; f != nil {
		f.Close()
		delete(linuxCardAccess.files, p)
	}
}
func retainedLinuxCard(p string) (source.Source, error) {
	linuxCardAccess.Lock()
	f := linuxCardAccess.files[p]
	if f == nil {
		linuxCardAccess.Unlock()
		return nil, os.ErrPermission
	}
	old, e := f.Stat()
	now, statErr := os.Stat(p)
	if e != nil || statErr != nil || !os.SameFile(old, now) {
		f.Close()
		delete(linuxCardAccess.files, p)
		linuxCardAccess.Unlock()
		return nil, fmt.Errorf("内存卡已拔出或更换")
	}
	fd, e := syscall.Dup(int(f.Fd()))
	if e == nil {
		syscall.CloseOnExec(fd)
	}
	linuxCardAccess.Unlock()
	if e != nil {
		return nil, e
	}
	s, e := source.FromFile(os.NewFile(uintptr(fd), p))
	if e != nil {
		forgetLinuxCard(p)
	}
	return s, e
}
func pruneLinuxCards(candidates []Device) {
	present := map[string]bool{}
	for _, d := range candidates {
		present[d.Path] = true
	}
	linuxCardAccess.Lock()
	defer linuxCardAccess.Unlock()
	for p, f := range linuxCardAccess.files {
		if !present[p] {
			f.Close()
			delete(linuxCardAccess.files, p)
		}
	}
}

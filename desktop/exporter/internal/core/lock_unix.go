//go:build darwin || linux

package core

import (
	"errors"
	"os"
	"syscall"
)

func lockFile(f *os.File) error         { return syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB) }
func lockBusy(err error) bool           { return errors.Is(err, syscall.EWOULDBLOCK) }
func hideControlDirectory(string) error { return nil }

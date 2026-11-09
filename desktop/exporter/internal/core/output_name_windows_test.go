package core

import (
	"os"
	"path/filepath"
	"syscall"
	"testing"
)

func TestOutputControlDirectoryHiddenOnWindows(t *testing.T) {
	base := t.TempDir()
	root, err := os.OpenRoot(base)
	if err != nil {
		t.Fatal(err)
	}
	defer root.Close()
	f, err := managedLock(root, "output/2026-11-09 17时30分28秒")
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	p, err := syscall.UTF16PtrFromString(filepath.Join(base, controlDirectory))
	if err != nil {
		t.Fatal(err)
	}
	flags, err := syscall.GetFileAttributes(p)
	if err != nil || flags&syscall.FILE_ATTRIBUTE_HIDDEN == 0 {
		t.Fatal("control directory visible", flags, err)
	}
}

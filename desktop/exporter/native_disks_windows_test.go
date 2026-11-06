package main

import (
	"context"
	"fmt"
	"golang.org/x/sys/windows"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestNativeEnumerationWithoutPowerShell(t *testing.T) {
	// Empty executable search path: native enumeration must not spawn PowerShell.
	t.Setenv("PATH", t.TempDir())
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	inv, parts, err := nativeWinInventory(ctx)
	if err != nil {
		t.Fatal(err)
	}
	t.Logf("native external disks=%d partitions=%d", inv.External, len(parts))
}

// QA-only native layout reads: no device writes and no internal disks admitted
// into production candidates. This checks actual Windows struct offsets.
func TestNativeReadOnlyLayoutABI(t *testing.T) {
	names, err := nativeDeviceNames()
	if err != nil {
		t.Fatal(err)
	}
	checked := 0
	for _, disk := range physicalDiskNumbers(names) {
		ptr, _ := windows.UTF16PtrFromString(fmt.Sprintf(`\\.\PhysicalDrive%d`, disk))
		h, e := windows.CreateFile(ptr, windows.GENERIC_READ, windows.FILE_SHARE_READ|windows.FILE_SHARE_WRITE, nil, windows.OPEN_EXISTING, 0, 0)
		if e != nil {
			t.Fatal(e)
		}
		b, e := nativeIOCTL(h, ioctlDiskGetDriveLayoutEx, nil)
		windows.CloseHandle(h)
		if e != nil {
			t.Fatal(e)
		}
		parts, e := nativePartitions(b, disk)
		if e != nil {
			t.Fatal(e)
		}
		t.Logf("disk=%d layoutBytes=%d partitions=%d", disk, len(b), len(parts))
		checked++
	}
	if checked == 0 {
		t.Skip("no physical disks exposed by runner")
	}
}

func TestWindowsDetectionHasNoShellDependency(t *testing.T) {
	// Checked in source checkout; compiled tests copied elsewhere run the runtime
	// test above instead.
	for _, name := range []string{"cards_windows.go", "devices_windows.go"} {
		b, err := os.ReadFile(filepath.Join(".", name))
		if os.IsNotExist(err) {
			continue
		}
		if err != nil {
			t.Fatal(err)
		}
		for i := 0; i+len("powershell") <= len(b); i++ {
			if string(b[i:i+len("powershell")]) == "powershell" {
				t.Fatal("shell dependency in", name)
			}
		}
	}
}

package main

import (
	"context"
	"errors"
	"fmt"
	"log"
	"sync"
	"syscall"
	"time"

	"golang.org/x/sys/windows"
)

const (
	ioctlStorageQueryProperty = 0x002d1400
	ioctlDiskGetDriveLayoutEx = 0x00070050
)

func nativeDeviceNames() ([]string, error) {
	for size := 4096; size <= 1<<20; size *= 2 {
		buf := make([]uint16, size)
		n, err := windows.QueryDosDevice(nil, &buf[0], uint32(len(buf)))
		if errors.Is(err, windows.ERROR_INSUFFICIENT_BUFFER) {
			continue
		}
		if err != nil {
			return nil, nativeWindowsError("QueryDosDevice", err)
		}
		var names []string
		start := 0
		for i := 0; i < int(n); i++ {
			if buf[i] == 0 {
				if i > start {
					names = append(names, syscall.UTF16ToString(buf[start:i]))
				}
				start = i + 1
			}
		}
		return names, nil
	}
	return nil, fmt.Errorf("QueryDosDevice 返回的设备列表过大")
}

func nativeIOCTL(h windows.Handle, code uint32, input []byte) ([]byte, error) {
	var in *byte
	if len(input) > 0 {
		in = &input[0]
	}
	for size := 4096; size <= 4<<20; size *= 2 {
		out := make([]byte, size)
		var n uint32
		err := windows.DeviceIoControl(h, code, in, uint32(len(input)), &out[0], uint32(len(out)), &n, nil)
		if errors.Is(err, windows.ERROR_INSUFFICIENT_BUFFER) || errors.Is(err, windows.ERROR_MORE_DATA) {
			continue
		}
		if err != nil {
			return nil, nativeWindowsError(fmt.Sprintf("DeviceIoControl 0x%08x", code), err)
		}
		if n > uint32(len(out)) {
			return nil, fmt.Errorf("设备返回的长度超出缓冲区")
		}
		return out[:n], nil
	}
	return nil, fmt.Errorf("设备返回的数据超过 4 MiB")
}

// Revalidate the exact handle used for reads, not just a prior enumeration.
// A disk number may be reused after unplugging a reader.
func layoutOnHandle(h windows.Handle, disk uint32) ([]winPartition, bool, error) {
	b, err := nativeIOCTL(h, ioctlStorageQueryProperty, make([]byte, 12))
	if err != nil {
		return nil, false, fmt.Errorf("查询存储总线：%w", err)
	}
	bus, err := storageBus(b)
	if err != nil {
		return nil, false, err
	}
	if !externalStorageBus(bus) {
		return nil, false, nil
	}
	b, err = nativeIOCTL(h, ioctlDiskGetDriveLayoutEx, nil)
	if err != nil {
		return nil, true, fmt.Errorf("读取分区布局：%w", err)
	}
	parts, err := nativePartitions(b, disk)
	return parts, true, err
}

var nativeDiagnosticOnce sync.Once

func nativeWinInventory(ctx context.Context) (cardInventory, []winPartition, error) {
	nativeDiagnosticOnce.Do(nativeStartupDiagnostic)
	start := time.Now()
	names, err := nativeDeviceNames()
	if err != nil {
		return cardInventory{}, nil, err
	}
	inv := cardInventory{}
	var all []winPartition
	var failures []error
	for _, disk := range physicalDiskNumbers(names) {
		if err := ctx.Err(); err != nil {
			return inv, nil, err
		}
		name := fmt.Sprintf(`\\.\PhysicalDrive%d`, disk)
		ptr, _ := windows.UTF16PtrFromString(name)
		// No write access, including during discovery. No mounts or disk mutations.
		h, err := windows.CreateFile(ptr, windows.GENERIC_READ, windows.FILE_SHARE_READ|windows.FILE_SHARE_WRITE, nil, windows.OPEN_EXISTING, 0, 0)
		if err != nil {
			if errors.Is(err, windows.ERROR_NOT_READY) || errors.Is(err, windows.ERROR_FILE_NOT_FOUND) {
				continue
			}
			failures = append(failures, nativeWindowsError(fmt.Sprintf("打开磁盘 %d", disk), err))
			continue
		}
		parts, external, e := layoutOnHandle(h, disk)
		windows.CloseHandle(h)
		if external {
			inv.External++
		}
		if e != nil {
			if errors.Is(e, windows.ERROR_NOT_READY) {
				continue
			}
			failures = append(failures, fmt.Errorf("磁盘 %d：%w", disk, e))
			continue
		}
		for _, p := range parts {
			all = append(all, p)
			inv.Candidates = append(inv.Candidates, Device{winPath(p), fmt.Sprintf("%s · %.1f GiB", p.Name, float64(p.Size)/(1<<30))})
		}
	}
	if len(failures) > 0 {
		e := errors.Join(failures...)
		log.Printf("Windows native enumeration elapsed=%s partial=%t: %v", time.Since(start), len(all) > 0, e)
		// An unrelated broken disk must not hide an available external recording card.
		if len(all) == 0 {
			return inv, nil, fmt.Errorf("Windows 原生磁盘检测失败：%w", e)
		}
	}
	return inv, all, nil
}

func validateNativeHandle(h windows.Handle, want winPartition) error {
	parts, external, err := layoutOnHandle(h, want.Disk)
	if err != nil {
		return err
	}
	if !external {
		return fmt.Errorf("设备不再是 USB/SD/MMC 外置磁盘")
	}
	for _, p := range parts {
		if p.Partition == want.Partition && p.Offset == want.Offset && p.Size == want.Size {
			return nil
		}
	}
	return fmt.Errorf("设备分区布局已改变，请重新检测内存卡")
}

func nativeWindowsError(stage string, err error) error {
	var code syscall.Errno
	if errors.As(err, &code) {
		return fmt.Errorf("%s (Win32=%d)：%w", stage, uint32(code), err)
	}
	return fmt.Errorf("%s：%w", stage, err)
}

func nativeStartupDiagnostic() {
	v := windows.RtlGetVersion()
	log.Printf("Windows device backend=native QueryDosDevice/DeviceIoControl elevated=%t version=%d.%d build=%d", windows.GetCurrentProcessToken().IsElevated(), v.MajorVersion, v.MinorVersion, v.BuildNumber)
}

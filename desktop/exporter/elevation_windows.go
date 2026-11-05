package main

import (
	"fmt"
	"os"
	"strings"
	"syscall"
	"unsafe"
)

const elevationMarker = "--openaria-elevated"

func ensureElevated() (bool, error) {
	k := syscall.NewLazyDLL("kernel32.dll")
	a := syscall.NewLazyDLL("advapi32.dll")
	process, _, _ := k.NewProc("GetCurrentProcess").Call()
	var token syscall.Handle
	ok, _, e := a.NewProc("OpenProcessToken").Call(process, 8, uintptr(unsafe.Pointer(&token)))
	if ok == 0 {
		return true, e
	}
	defer syscall.CloseHandle(token)
	var elevated, size uint32
	ok, _, e = a.NewProc("GetTokenInformation").Call(uintptr(token), 20, uintptr(unsafe.Pointer(&elevated)), 4, uintptr(unsafe.Pointer(&size)))
	if ok == 0 {
		return true, e
	}
	marked := len(os.Args) > 1 && os.Args[1] == elevationMarker
	if elevationDecision(elevated != 0, marked) == 0 {
		if marked {
			os.Args = append(os.Args[:1], os.Args[2:]...)
		}
		return false, nil
	}
	if elevationDecision(elevated != 0, marked) == -1 {
		return true, fmt.Errorf("UAC 提升未生效，已停止以避免循环")
	}
	exe, e := os.Executable()
	if e != nil {
		return true, e
	}
	args := []string{elevationMarker}
	args = append(args, os.Args[1:]...)
	for i := range args {
		args[i] = syscall.EscapeArg(args[i])
	}
	verb, _ := syscall.UTF16PtrFromString("runas")
	file, e := syscall.UTF16PtrFromString(exe)
	if e != nil {
		return true, e
	}
	params, e := syscall.UTF16PtrFromString(strings.Join(args, " "))
	if e != nil {
		return true, e
	}
	cwd, e := os.Getwd()
	if e != nil {
		return true, e
	}
	dir, e := syscall.UTF16PtrFromString(cwd)
	if e != nil {
		return true, e
	}
	type shellExecuteInfo struct {
		Size, Mask                        uint32
		Window                            uintptr
		Verb, File, Parameters, Directory *uint16
		Show                              int32
		Instance                          uintptr
		IDList                            uintptr
		Class                             *uint16
		ClassKey                          uintptr
		HotKey                            uint32
		Icon, Process                     uintptr
	}
	info := shellExecuteInfo{Mask: 0x40 | 0x100, Verb: verb, File: file, Parameters: params, Directory: dir, Show: 1}
	info.Size = uint32(unsafe.Sizeof(info))
	ok, _, e = syscall.NewLazyDLL("shell32.dll").NewProc("ShellExecuteExW").Call(uintptr(unsafe.Pointer(&info)))
	if ok == 0 {
		if e == syscall.Errno(1223) {
			fmt.Fprintln(os.Stderr, "已取消管理员授权；应用未启动。")
			return true, nil
		}
		return true, fmt.Errorf("启动管理员进程失败: %w", e)
	}
	if info.Process != 0 {
		syscall.CloseHandle(syscall.Handle(info.Process))
	}
	return true, nil
}

func startupError(err error) {
	fmt.Fprintln(os.Stderr, err)
	text, _ := syscall.UTF16PtrFromString(err.Error())
	title, _ := syscall.UTF16PtrFromString("Open Aria Exporter")
	syscall.NewLazyDLL("user32.dll").NewProc("MessageBoxW").Call(0, uintptr(unsafe.Pointer(text)), uintptr(unsafe.Pointer(title)), 0x10)
}

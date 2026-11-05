package main

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"time"

	"github.com/egoist/mygo"
	"openaria-exporter/internal/privileged"
)

const helperLocation = "/Library/PrivilegedHelperTools/dev.alpen.openaria-exporter.helper"
const privacyURL = "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"
const accessSteps = "1. 通过 PKG 安装独立只读服务。管理员安装授权不等于隐私授权。\n2. 打开完整磁盘访问设置，用 Finder 定位下方 helper，将它加入列表并开启完整磁盘访问（FDA）：\n" + helperLocation + "\n\n完成后连接设备，选择菜单‘重新检查设备访问’。关闭此说明后，菜单入口仍保留。检查成功仅表示当前设备可读，不记录 FDA 授权成功。"

// Protocol v1 exposes strings, not errno. Match the complete service response;
// local errors, nested diskutil errors and other permission errors are not TCC evidence.
var serviceEPERM = regexp.MustCompile(`^request=[A-Za-z0-9-]{1,64}: operation not permitted$`)

func devicePrivacyHint(err error) string {
	if err == nil || !serviceEPERM.MatchString(err.Error()) {
		return ""
	}
	return "。服务返回 EPERM；可能需要隐私授权，请从菜单‘设备访问配置…’打开完整磁盘访问设置，将 " + helperLocation + " 加入并开启 FDA；管理员安装授权不等于隐私授权。此错误本身不能确定为 TCC 拒绝"
}

type accessReader interface {
	io.ReaderAt
	io.Closer
}
type accessResult struct {
	message string
	err     error
}

func probeDevice(f accessReader) (err error) {
	defer func() { err = errors.Join(err, f.Close()) }()
	b := make([]byte, 512)
	n, err := f.ReadAt(b, 0)
	if err == nil && n != len(b) {
		err = io.ErrUnexpectedEOF
	}
	return err
}

func checkDeviceAccess(ctx context.Context, selected string, status func(context.Context) error, list func() ([]Device, error), open func(context.Context, string) (accessReader, error)) accessResult {
	if err := status(ctx); err != nil {
		return accessResult{"服务连接失败；请确认 PKG 服务已安装且可用", err}
	}
	const connected = "服务已连接（仅确认服务连接，不代表隐私授权或设备可读）。"
	ds, err := list()
	if err != nil {
		return accessResult{connected + "设备枚举失败", err}
	}
	if len(ds) == 0 {
		return accessResult{message: connected + "等待连接外置 Linux 设备。"}
	}
	p := ds[0].Path
	for _, d := range ds {
		if d.Path == selected {
			p = selected
			break
		}
	}
	f, err := open(ctx, p)
	if err != nil {
		return accessResult{connected + "当前设备访问失败：" + p, err}
	}
	if err = probeDevice(f); err != nil {
		return accessResult{connected + "当前设备读取或关闭失败：" + p, err}
	}
	return accessResult{message: "当前设备可读：" + p + "（新句柄读取 512 字节并已关闭）。"}
}

func (a *app) deviceAccessMenu() []*mygo.MenuItem {
	return []*mygo.MenuItem{
		{Label: "设备访问配置…", Click: func(*mygo.MenuItem, *mygo.Window) { a.showDeviceAccess() }},
		{Label: "打开完整磁盘访问设置…", Click: func(*mygo.MenuItem, *mygo.Window) { a.openAccessLocation(false) }},
		{Label: "在 Finder 中定位 helper…", Click: func(*mygo.MenuItem, *mygo.Window) { a.openAccessLocation(true) }},
		{Label: "重新检查设备访问", Click: func(*mygo.MenuItem, *mygo.Window) { a.recheckDeviceAccess() }},
	}
}
func (a *app) showDeviceAccess() { a.notice("设备访问配置（两步）", accessSteps) }
func (a *app) openAccessLocation(reveal bool) {
	args := []string{privacyURL}
	if reveal {
		args = []string{"-R", helperLocation}
	}
	go func() {
		if b, err := exec.Command("/usr/bin/open", args...).CombinedOutput(); err != nil {
			log.Printf("stage=device-access-settings reason=%v output=%s", err, b)
			a.notice("无法打开配置位置", fmt.Sprintf("%v\n%s\n%s", err, b, logLocation()))
		}
	}()
}

// This marker means only that the explanation was displayed, never authorization.
func guideNeeded(marker string) bool {
	_, err := os.Stat(marker)
	return errors.Is(err, os.ErrNotExist)
}
func markGuideShown(marker string) error {
	if err := os.MkdirAll(filepath.Dir(marker), 0700); err != nil {
		return err
	}
	return os.WriteFile(marker, []byte("explanation-shown-v1\n"), 0600)
}
func (a *app) firstDeviceAccessGuide() {
	if _, err := os.Stat(helperLocation); err != nil {
		return
	}
	dir, err := os.UserConfigDir()
	if err != nil {
		log.Printf("stage=device-access-guide reason=%v", err)
		return
	}
	marker := filepath.Join(dir, "OpenAriaExporter", "device-access-guide-v1-shown")
	if !guideNeeded(marker) {
		return
	}
	go func() {
		_, err := mygo.Dialog.Message(mygo.MessageOptions{Message: "设备访问配置（两步）", Detail: accessSteps, Buttons: []string{"知道了"}})
		if err == nil {
			err = markGuideShown(marker)
		}
		if err != nil {
			log.Printf("stage=device-access-guide reason=%v", err)
		}
	}()
}
func (a *app) recheckDeviceAccess() {
	if a.busy {
		a.notice("任务进行中", "请等待扫描、导出或检查完成后，再检查设备访问。")
		return
	}
	a.busy = true
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	a.cancel = cancel
	selected := a.src
	a.setStatus("正在检查服务连接与当前设备读取…", false)
	go func() {
		defer cancel()
		result := checkDeviceAccess(ctx, selected, func(ctx context.Context) error { _, err := privileged.RequestFile(ctx, "status", ""); return err }, devices, func(ctx context.Context, p string) (accessReader, error) {
			return privileged.RequestFile(ctx, "open", p)
		})
		if result.err != nil {
			log.Printf("stage=device-access-check reason=%v", result.err)
			result.message += "：" + result.err.Error() + devicePrivacyHint(result.err) + "。" + logLocation()
		}
		a.update(func() {
			a.busy = false
			a.cancel = nil
			a.setStatus(result.message, result.err != nil)
			a.notice("设备访问检查", result.message)
		})
	}()
}

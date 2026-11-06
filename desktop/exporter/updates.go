package main

import (
	"fmt"
	"github.com/egoist/mygo"
	"github.com/egoist/mygo/plugins/updater"
	"github.com/egoist/mygo/plugins/updater/native"
)

// Set only by scripts/package.py after validating the release configuration.
var updatesConfigured = ""

func enforceManualUpdates() {
	if updater.AutomaticChecks() {
		updater.SetAutomaticChecks(false)
	}
	if updater.AutomaticDownloads() {
		updater.SetAutomaticDownloads(false)
	}
}
func (a *app) setupUpdates() {
	mygo.Use(native.New(updater.Options{DisableAutomaticChecks: true, Language: "zh-Hans", Strings: map[string]updater.Strings{"zh-Hans": {AutomaticDownloads: "自动下载已由应用禁用（此选项不会生效）"}}}))
	// Load and override persisted choices before WhenReady starts the scheduler.
	updater.SetAutomaticChecks(false)
	updater.SetAutomaticDownloads(false)
	updater.OnChange(enforceManualUpdates)
	mygo.App.OnBeforeQuit(a.beforeQuit)
	mygo.App.SetMenu(mygo.NewMenu([]*mygo.MenuItem{
		{Label: "Open Aria Exporter", Submenu: append([]*mygo.MenuItem{
			{Label: "关于 Open Aria Exporter", Click: func(*mygo.MenuItem, *mygo.Window) {
				a.notice(fmt.Sprintf("Open Aria Exporter %s", mygo.App.Version()), "只读扫描与录像导出。更新仅由手动检查和安装触发。")
			}},
			{Label: "检查更新…", Click: func(*mygo.MenuItem, *mygo.Window) { a.checkUpdates() }},
			{Label: "导出诊断信息…", Click: func(*mygo.MenuItem, *mygo.Window) { a.exportDiagnostics() }},
			{Label: "打开日志文件…", Click: func(*mygo.MenuItem, *mygo.Window) { a.openLogs() }},
		}, append(a.deviceAccessMenu(), mygo.Separator(), &mygo.MenuItem{Role: mygo.RoleQuit})...)}, {Role: mygo.RoleEditMenu}, {Role: mygo.RoleWindowMenu},
	}))
}
func (a *app) notice(title, detail string) {
	go func() { _, _ = mygo.Dialog.Message(mygo.MessageOptions{Message: title, Detail: detail}) }()
}
func updateUnavailable(dev, configured, enabled bool) string {
	if dev {
		return "当前为开发运行，不能安装更新。请使用打包后的应用。"
	}
	if !configured {
		return "此安装包未配置更新地址和公钥；可离线使用，请手动安装后续版本。"
	}
	if !enabled {
		return "当前位置无法自更新（不可写、包管理器安装或非应用目录）。请移至可写安装位置，或使用安装器／包管理器更新。"
	}
	return ""
}
func (a *app) checkUpdates() {
	if a.busy {
		a.notice("任务进行中", "请等待扫描或导出完成，或取消任务并等待清理后，再检查更新。")
		return
	}
	enforceManualUpdates()
	if reason := updateUnavailable(mygo.IsDev(), updatesConfigured == "1", mygo.Updater.Enabled()); reason != "" {
		a.notice("更新不可用", reason)
		return
	}
	updater.CheckForUpdates()
}

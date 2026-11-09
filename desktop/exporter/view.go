package main

import (
	"fmt"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"

	"github.com/egoist/mygo/ui"
	"openaria-exporter/internal/core"
)

// Lucide 风格的线条图标，随文字颜色着色。
func icon(body string) *ui.SVG {
	return ui.MustParseSVG([]byte(`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">` + body + `</svg>`))
}

var (
	iconFolder  = icon(`<path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/>`)
	iconDisk    = icon(`<line x1="22" x2="2" y1="12" y2="12"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/><line x1="6" x2="6.01" y1="16" y2="16"/><line x1="10" x2="10.01" y1="16" y2="16"/>`)
	iconImage   = icon(`<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/><ellipse cx="12" cy="13" rx="4" ry="2"/><path d="M8 13v3c0 1.1 1.8 2 4 2s4-.9 4-2v-3"/>`)
	iconRefresh = icon(`<path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/><path d="M8 16H3v5"/>`)
	iconScan    = icon(`<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>`)
	iconVideo   = icon(`<path d="m16 13 5.22 3.48a.5.5 0 0 0 .78-.42V7.94a.5.5 0 0 0-.78-.42L16 11"/><rect x="2" y="6" width="14" height="12" rx="2"/>`)
	iconExport  = icon(`<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" x2="12" y1="15" y2="3"/>`)
	iconOpen    = icon(`<path d="M15 3h6v6"/><path d="M10 14 21 3"/><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>`)
	iconShield  = icon(`<path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/>`)
	iconAlert   = icon(`<circle cx="12" cy="12" r="10"/><line x1="12" x2="12" y1="8" y2="12"/><line x1="12" x2="12.01" y1="16" y2="16"/>`)
	iconCheck   = icon(`<circle cx="12" cy="12" r="10"/><path d="m9 12 2 2 4-4"/>`)
	iconInbox   = icon(`<polyline points="22 12 16 12 14 15 10 15 8 12 2 12"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/>`)
)

const sidebarWidth = 300

func mib(n int64) string {
	switch {
	case n >= 1<<30:
		return fmt.Sprintf("%.2f GiB", float64(n)/(1<<30))
	default:
		return fmt.Sprintf("%.1f MiB", float64(n)/(1<<20))
	}
}

func (a *app) view(c *ui.Context) {
	t := c.Theme()
	ui.Row(c).Fill().AlignItems(ui.Stretch).Children(func() {
		ui.Column(c).Width(sidebarWidth).Background(t.Surface).BorderWidth(0, 1, 0, 0).BorderColor(t.Border).Children(func() {
			a.sidebar(c)
		})
		ui.Column(c).Grow(1).Children(func() {
			a.content(c)
			a.actionBar(c)
		})
	})
	a.localActionDialog(c)
	a.exportWarningDialog(c)
}

func sectionTitle(c *ui.Context, s string) ui.Element {
	return ui.Text(c, s).FontSize(11).FontWeight(600).LetterSpacing(0.6).TextColor(c.Theme().TextMuted)
}

// sidebar 选择数据源与导出设置。
func (a *app) sidebar(c *ui.Context) {
	t := c.Theme()
	bar := c.TitleBar()
	ui.Row(c).Height(max(bar.Height, 12) + 8).DragWindow()
	ui.Scroll(c).Grow(1).Children(func() {
		ui.Column(c).Padding(4, 20, 20, 20).Gap(20).Children(func() {
			ui.Row(c).Gap(10).Children(func() {
				ui.Box(c).Size(34, 34).Radius(9).Center().Gradient(t.Accent, t.Accent.Mix(ui.Hex("#7c3aed"), 0.55), 135).Children(func() {
					ui.Icon(c, iconVideo).FontSize(18).TextColor(ui.RGB(255, 255, 255))
				})
				ui.Column(c).Gap(1).Children(func() {
					ui.Text(c, "Open Aria").FontSize(15).Bold()
					ui.Text(c, "录像导出").FontSize(12).TextColor(t.TextMuted)
				})
			})

			ui.Column(c).Gap(10).Children(func() {
				ui.Row(c).Children(func() {
					sectionTitle(c, "OpenAria 内存卡").Grow(1)
					ui.Button(c, "").Label("重新检测内存卡").Tooltip("重新检测内存卡").Disabled(a.busy || a.refreshing).Padding(4).Children(func() {
						ui.Icon(c, iconRefresh).FontSize(13)
					}).OnClick(func() { a.refreshCards(true) })
				})
				options := make([]string, 0, len(a.cards))
				selected := ""
				for _, card := range a.cards {
					options = append(options, card.Label)
					if card.Path == a.src && card.VolumeID == a.cardID {
						selected = card.Label
					}
				}
				if len(options) == 0 {
					options = []string{"请插入 OpenAria 内存卡"}
					selected = options[0]
				}
				if a.cardChoice == "" {
					a.cardChoice = selected
				}
				ui.Select(c.Key("card-selector"), &a.cardChoice, options).Label("内存卡").Disabled(a.busy || a.refreshing || len(a.cards) == 0).FontSize(12)
				if a.cardChoice != selected {
					for _, card := range a.cards {
						if card.Label == a.cardChoice {
							a.selectCard(card)
							break
						}
					}
				}
				ui.Text(c, a.cardMessage).FontSize(11).TextColor(t.TextMuted)
				ui.Button(c, "重新扫描录像").Disabled(a.busy || a.refreshing || a.src == "").OnClick(func() { a.run(false) })
			})

			ui.Divider(c)

			ui.Column(c).Gap(10).Children(func() {
				sectionTitle(c, "导出设置")
				ui.Column(c).Gap(6).Children(func() {
					ui.Text(c, "保存位置").FontSize(12).FontWeight(500)
					ui.Row(c).Gap(6).Children(func() {
						ui.TextInput(c.Key("output-path"), &a.out).Label("保存位置").Placeholder("选择保存位置").ReadOnly(true).Disabled(a.busy).FontSize(12).Grow(1)
						ui.Button(c, "").Label("选择输出目录").Tooltip("选择输出目录").Disabled(a.busy).Padding(6).Children(func() {
							ui.Icon(c, iconFolder).FontSize(14)
						}).OnClick(a.chooseOutput)
					})
				})
				ui.Text(c, "自动保存到所选位置下的 OpenAriaExporter 文件夹").FontSize(11).TextColor(t.TextMuted)
				ui.Text(c, "导出 MP4 及全部原始视频、IMU、音频；处理失败仍保存原始数据并提示").FontSize(11).TextColor(t.TextMuted)
			})
		})
	})
	ui.Row(c).Gap(8).Padding(12, 20).BorderWidth(1, 0, 0, 0).BorderColor(t.Border).Children(func() {
		ui.Icon(c, iconShield).FontSize(13).TextColor(t.Success)
		ui.Text(c, "只读访问内存卡 · 保留卡内原始录像").FontSize(11).TextColor(t.TextMuted).Grow(1)
	})
}

func (a *app) filtered() []core.Session {
	q := strings.ToLower(a.query)
	var rows []core.Session
	for _, v := range a.items {
		if strings.Contains(strings.ToLower(v.Name+v.ID), q) {
			rows = append(rows, v)
		}
	}
	return rows
}

// content 显示录像列表。
func (a *app) content(c *ui.Context) {
	t := c.Theme()
	rows := a.filtered()
	bar := c.TitleBar()
	ui.Column(c).Padding(max(bar.Height, 12)+8, 24, 12, 24).Gap(14).DragWindow().Children(func() {
		ui.Row(c).Gap(12).Children(func() {
			ui.Row(c).Gap(8).AlignItems(ui.Baseline).Grow(1).Children(func() {
				ui.Text(c, "录像").FontSize(22).Bold()
				if len(a.items) > 0 {
					ui.Textf(c, "%d 条", len(a.items)).FontSize(13).TextColor(t.TextMuted)
				}
			})
			ui.SearchField(c, &a.query).Label("搜索录像").Placeholder("搜索名称或 session_id").Width(280)
		})
		ui.Row(c).Gap(8).Children(func() {
			if ui.Button(c, "全选搜索结果").Disabled(a.busy || len(rows) == 0).Clicked() {
				for _, v := range rows {
					if v.Error == "" {
						a.picked[v.Dir] = true
					}
				}
			}
			if ui.Button(c, "清空选择").Disabled(a.busy || len(a.picked) == 0).Clicked() {
				a.picked = map[string]bool{}
			}
			ui.Button(c.Key("delete-outputs"), "删除本机成品").Disabled(a.busy || a.refreshing || len(a.picked) == 0).OnClick(func() { a.manageLocal("outputs") })
			ui.Button(c.Key("clear-marks"), "清除标记").Disabled(a.busy || a.refreshing || len(a.picked) == 0).OnClick(func() { a.manageLocal("marks") })
			ui.Spacer(c)
			var bytes int64
			for _, v := range a.items {
				if a.picked[v.Dir] {
					bytes += v.Bytes
				}
			}
			chip := ui.Row(c).Gap(6).Padding(5, 10).Radius(999).Background(t.Surface)
			if len(a.picked) > 0 {
				chip.Background(t.Accent.Alpha(0.12))
			}
			chip.Children(func() {
				ui.Textf(c, "已选 %d / %d", len(a.picked), len(a.items)).FontSize(12).FontWeight(600).FontFeatures("tnum")
				ui.Text(c, "·").FontSize(12).TextColor(t.TextMuted)
				ui.Text(c, mib(bytes)).FontSize(12).TextColor(t.TextMuted).FontFeatures("tnum")
			})
		})
	})
	ui.List(c.Key("recordings"), &a.list, len(rows), func(i int) {
		a.recordingRow(c, rows[i])
	}).Grow(1).Gap(6).Padding(0, 24, 16, 24).Children(func() {
		if len(rows) == 0 {
			a.emptyState(c)
		}
	})
}

func (a *app) emptyState(c *ui.Context) {
	t := c.Theme()
	title, hint := "插入内存卡即可开始", "自动识别 OpenAria 内存卡，读取其中的录像。"
	if a.scanned != "" {
		title, hint = "这张内存卡还没有录像", "录制完成后重新插入内存卡，或点击重新扫描录像。"
	}
	if len(a.items) > 0 {
		title, hint = "没有匹配的录像", "换个关键词试试。"
	} else if a.busy {
		title, hint = "正在扫描…", "大容量设备可能需要一些时间。"
	}
	ui.Column(c).Grow(1).Center().Gap(8).Padding(80, 24).Children(func() {
		ui.Box(c).Size(56, 56).Radius(16).Center().Background(t.Surface).Children(func() {
			if a.busy && len(a.items) == 0 {
				ui.Spinner(c)
			} else {
				ui.Icon(c, iconInbox).FontSize(24).TextColor(t.TextMuted)
			}
		})
		ui.Text(c, title).FontSize(15).FontWeight(600).Margin(6, 0, 0, 0)
		ui.Text(c, hint).FontSize(12).TextColor(t.TextMuted)
	})
}

func (a *app) recordingRow(c *ui.Context, v core.Session) {
	t := c.Theme()
	selected := a.picked[v.Dir]
	bad := v.Error != ""
	row := ui.Row(c.Key("row-"+v.Dir)).Gap(14).Padding(12, 16).Radius(10).Border(1, t.Border).Background(t.Background)
	if selected {
		row.Background(t.Accent.Alpha(0.08)).BorderColor(t.Accent.Alpha(0.6))
	} else if row.Hovered() && !bad && !a.busy {
		row.Background(t.Surface)
	}
	row.Children(func() {
		ui.Checkbox(c.Key(v.Dir), &selected, "").Label(v.Name + " · " + v.ID).Disabled(a.busy || bad)
		ui.Box(c).Size(36, 36).Radius(8).Center().Background(t.Surface).Children(func() {
			ic := ui.Icon(c, iconVideo).FontSize(17).TextColor(t.Accent)
			if bad {
				ic.TextColor(t.TextMuted)
			}
		})
		ui.Column(c).Grow(1).Shrink(1).Gap(3).Children(func() {
			name := v.Name
			if name == "" {
				name = "未命名录像"
			}
			ui.Text(c, name).FontSize(14).FontWeight(600).SingleLine()
			ui.Text(c, v.ID).FontSize(11).Font("monospace").TextColor(t.TextMuted).SingleLine()
			if bad {
				ui.Text(c, "不可导出："+v.Error).FontSize(11).TextColor(t.Danger).MaxLines(2)
			}
		})
		ui.Column(c).AlignItems(ui.End).Gap(5).Children(func() {
			if result := a.exportResults[v.Dir]; result != "" {
				color := t.Warning
				if result == "本次已导出" {
					color = t.Success
				}
				detail := result
				if v.LocalExportedAt != "" {
					detail += "\n历史成功记录：" + v.LocalExportedAt + "\n" + v.LocalExportPath
				}
				ui.Badge(c, result).FontSize(11).Background(color.Alpha(0.14)).TextColor(color).Tooltip(detail)
			} else if v.LocalExportedAt != "" {
				ui.Badge(c, "本机已导出").FontSize(11).Background(t.Success.Alpha(0.14)).TextColor(t.Success).Tooltip(v.LocalExportedAt + "\n" + v.LocalExportPath)
			}
			if v.Error != "" || strings.Contains(v.Status, "partial") {
				statusBadge(c, v)
			}
			ui.Textf(c, "%s · %s", v.TimeLabel(), mib(v.Bytes)).FontSize(11).TextColor(t.TextMuted).FontFeatures("tnum").SingleLine().Tooltip(v.TimeDetail())
		})
	})
	if !bad && !a.busy && row.Clicked() {
		selected = !selected
	}
	if selected {
		a.picked[v.Dir] = true
	} else {
		delete(a.picked, v.Dir)
	}
}

func statusBadge(c *ui.Context, v core.Session) {
	t := c.Theme()
	label, color := v.Status, t.TextMuted
	switch {
	case v.Error != "":
		label, color = "不可导出", t.Danger
	case strings.Contains(v.Status, "partial"):
		color = t.Warning
	}
	ui.Badge(c, label).FontSize(11).Background(color.Alpha(0.14)).TextColor(color)
}

// actionBar 显示进度与导出操作。
func (a *app) actionBar(c *ui.Context) {
	t := c.Theme()
	ready := !a.refreshing && a.held != nil && a.scanned == a.src && len(a.picked) > 0 && a.out != ""
	ui.Row(c).Gap(16).Padding(14, 24).Background(t.Surface).BorderWidth(1, 0, 0, 0).BorderColor(t.Border).Children(func() {
		ui.Column(c).Grow(1).Shrink(1).Gap(6).Children(func() {
			a.statusLine(c)
			if a.busy && a.exporting {
				ui.Progress(c, a.progress).Label("导出进度")
			}
		})
		if ui.Button(c, "").Label("打开目标").Tooltip(outputDirectory(a.out)).Disabled(a.out == "").Children(func() {
			ui.Icon(c, iconOpen).FontSize(13)
			ui.Text(c, "打开目标").SingleLine()
		}).Clicked() {
			openPath(outputDirectory(a.out))
		}
		if a.busy {
			if ui.Button(c.Key("cancel"), "取消").Disabled(a.cancel == nil).Clicked() {
				a.cancel()
			}
		}
		hint := ""
		switch {
		case a.held == nil || a.scanned != a.src:
			hint = "请插入 OpenAria 内存卡并等待扫描完成"
		case len(a.picked) == 0:
			hint = "请选择要导出的录像"
		case a.out == "":
			hint = "请选择输出目录"
		}
		btn := ui.PrimaryButton(c.Key("export"), "").Label("开始导出").Disabled(a.busy || !ready).Padding(8, 18).Children(func() {
			ui.Icon(c, iconExport).FontSize(13)
			ui.Text(c, "开始导出").SingleLine()
		})
		if hint != "" && !a.busy {
			btn.Tooltip(hint)
		}
		if btn.Clicked() {
			a.run(true)
		}
	})
}

func (a *app) statusLine(c *ui.Context) {
	t := c.Theme()
	ui.Row(c).Gap(8).Children(func() {
		switch {
		case a.busy:
			ui.Spinner(c).Size(14, 14)
		case a.failed:
			ui.Icon(c, iconAlert).FontSize(14).TextColor(t.Danger)
		case a.succeeded:
			ui.Icon(c, iconCheck).FontSize(14).TextColor(t.Success)
		}
		s := ui.Text(c, a.status).FontSize(12).MaxLines(2).Shrink(1)
		if a.failed {
			s.TextColor(t.Danger)
		} else if !a.busy && !a.succeeded {
			s.TextColor(t.TextMuted)
		}
	})
}

func openPath(p string) {
	go func() {
		name := "xdg-open"
		if runtime.GOOS == "darwin" {
			name = "open"
		}
		if runtime.GOOS == "windows" {
			name = "explorer.exe"
		}
		_ = exec.Command(name, filepath.Clean(p)).Run()
	}()
}

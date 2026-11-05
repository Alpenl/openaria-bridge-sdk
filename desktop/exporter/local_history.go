package main

import (
	"fmt"
	"strings"

	"github.com/egoist/mygo/ui"
	"openaria-exporter/internal/core"
	"openaria-exporter/internal/history"
)

var localHistoryPath = history.DefaultPath

func localHistory() (history.Store, error) {
	p, e := localHistoryPath()
	return history.Store{Path: p}, e
}
func loadLocalMarks(items []core.Session) error {
	s, e := localHistory()
	if e != nil {
		return e
	}
	return s.Apply(items)
}
func saveLocalMark(v core.Session, dst string) (history.Mark, error) {
	s, e := localHistory()
	if e != nil {
		return history.Mark{}, e
	}
	return s.Record(v, dst)
}

type localAction struct {
	store                        history.Store
	kind, title, message, detail string
	selected                     []core.Session
	outputs                      []history.Output
	open                         bool
}

func prepareLocalAction(s history.Store, kind string, selected []core.Session) (*localAction, error) {
	p := &localAction{store: s, kind: kind, selected: selected, open: true, title: "清除本机已导出标记", message: "只清除这台电脑的标记，保留卡内录像和本机成品。"}
	var details []string
	for _, v := range selected {
		details = append(details, v.Name+" · "+v.ID)
	}
	if kind == "outputs" {
		p.title = "删除本机导出的成品"
		p.message = "永久删除以下路径中的成品。本机已导出标记和卡内原始录像保留。"
		var err error
		p.outputs, err = s.Outputs(selected)
		if err != nil {
			return nil, err
		}
		if len(p.outputs) == 0 {
			return nil, fmt.Errorf("所选录像没有本机记录的成品；旧版本导出的文件请从保存目录管理")
		}
		details = nil
		for _, o := range p.outputs {
			if err = core.CheckLocalOutput(o.Receipt); err != nil {
				return nil, err
			}
			details = append(details, o.Path)
		}
	}
	p.detail = strings.Join(details, "\n\n")
	return p, nil
}
func (a *app) manageLocal(kind string) {
	if a.busy || a.refreshing || len(a.picked) == 0 {
		return
	}
	var selected []core.Session
	for _, v := range a.items {
		if a.picked[v.Dir] {
			selected = append(selected, v)
		}
	}
	if len(selected) == 0 {
		return
	}
	a.busy = true
	go func() {
		s, err := localHistory()
		var plan *localAction
		if err == nil {
			plan, err = prepareLocalAction(s, kind, selected)
		}
		a.update(func() {
			if err != nil {
				a.busy = false
				a.setStatus(err.Error(), true)
			} else {
				a.localAction = plan
			}
		})
	}()
}
func (a *app) localActionDialog(c *ui.Context) {
	p := a.localAction
	if p == nil {
		return
	}
	ui.Modal(c.Key("local-action-dialog"), &p.open, func() {
		ui.Column(c).Width(540).Padding(24).Gap(16).Children(func() {
			ui.Text(c, p.title).FontSize(18).Bold()
			ui.Text(c, p.message).FontSize(13)
			ui.Scroll(c).Height(230).Children(func() { ui.Text(c, p.detail).FontSize(12) })
			ui.Row(c).Gap(12).Children(func() {
				ui.Spacer(c)
				ui.Button(c.Key("cancel-local-action"), "取消").OnClick(func() { p.open = false })
				label := "确认清除标记"
				if p.kind == "outputs" {
					label = "确认永久删除"
				}
				ui.Button(c.Key("confirm-local-action"), label).TextColor(c.Theme().Danger).OnClick(func() { a.localAction = nil; a.executeLocalAction(p) })
			})
		})
	})
	if !p.open && a.localAction == p {
		a.localAction = nil
		a.busy = false
	}
}
func (a *app) executeLocalAction(p *localAction) {
	a.setStatus("正在"+p.title+"…", false)
	go func() {
		var err error
		if p.kind == "outputs" {
			completed := 0
			for _, o := range p.outputs {
				if err = core.DeleteLocalOutput(o.Receipt); err != nil {
					break
				}
				completed++
				if err = p.store.ForgetOutput(o.ID); err != nil {
					err = fmt.Errorf("成品已删除，但本机记录更新失败：%w", err)
					break
				}
			}
			if err != nil {
				err = fmt.Errorf("已删除 %d/%d 个成品：%w", completed, len(p.outputs), err)
			}
		} else {
			err = p.store.Clear(p.selected)
		}
		a.update(func() {
			a.busy = false
			if err != nil {
				a.setStatus(err.Error(), true)
				return
			}
			if p.kind != "outputs" {
				for i := range a.items {
					for _, v := range p.selected {
						if a.items[i].Dir == v.Dir {
							a.items[i].LocalExportedAt = ""
							a.items[i].LocalExportPath = ""
						}
					}
				}
			}
			a.setStatus(p.title+"完成", false)
		})
	}()
}

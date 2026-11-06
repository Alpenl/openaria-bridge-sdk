package main

import "github.com/egoist/mygo/ui"

type exportWarning struct {
	open          bool
	title, detail string
}

func (a *app) exportWarningDialog(c *ui.Context) {
	p := a.exportWarning
	if p == nil {
		return
	}
	ui.Modal(c.Key("export-warning"), &p.open, func() {
		ui.Column(c).Width(580).Padding(24).Gap(16).Children(func() {
			ui.Text(c, p.title).FontSize(18).Bold()
			ui.Scroll(c).Height(260).Children(func() { ui.Text(c, p.detail).FontSize(13) })
			ui.Button(c.Key("ack-export-warning"), "知道了").OnClick(func() { p.open = false })
		})
	})
	if !p.open {
		a.exportWarning = nil
	}
}

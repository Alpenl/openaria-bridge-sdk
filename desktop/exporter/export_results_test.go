package main

import (
	"github.com/egoist/mygo/ui"
	"openaria-exporter/internal/core"
	"testing"
)

func TestCurrentRawResultOverridesHistoricalSuccess(t *testing.T) {
	v := core.Session{Dir: "recording", ID: "one", Name: "录像", LocalExportedAt: "2026-11-09T17:39:03Z", LocalExportPath: "/previous/video"}
	a := &app{picked: map[string]bool{}, items: []core.Session{v}}
	a.applyExportResults(a.items, []string{"/new/raw"}, nil)
	tt := ui.NewTester(a.view, 1120, 760)
	if !tt.HasText("本次仅保存原始数据") || tt.HasText("本机已导出") || tt.HasText("本次已导出") {
		t.Fatal(tt.Texts())
	}
	if a.items[0].LocalExportedAt != v.LocalExportedAt || a.items[0].LocalExportPath != v.LocalExportPath {
		t.Fatal("historical mark changed by failed attempt")
	}
	a.applyExportResults(a.items, []string{"/new/video"}, map[string]bool{v.Dir: true})
	tt.Frame()
	if !tt.HasText("本次已导出") {
		t.Fatal("retry success not shown")
	}
	a.applyExportResults(a.items, nil, nil)
	tt.Frame()
	if !tt.HasText("本次未完成") {
		t.Fatal("cancellation not shown")
	}
	a.changeSource("new")
	if len(a.exportResults) != 0 {
		t.Fatal("old card results retained")
	}
}

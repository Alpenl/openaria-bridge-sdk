package main

import (
	"github.com/egoist/mygo/ui"
	"openaria-exporter/internal/core"
	"strings"
	"testing"
)

func TestSelectionSearchBusy(t *testing.T) {
	a := &app{picked: map[string]bool{}, items: []core.Session{{Dir: "a", ID: "one", Name: "苹果"}, {Dir: "b", ID: "two", Name: "香蕉"}}}
	tt := ui.NewTester(a.view, 1000, 780)
	if e := tt.Click("全选搜索结果"); e != nil {
		t.Fatal(e)
	}
	if len(a.picked) != 2 {
		t.Fatal(a.picked)
	}
	a.busy = true
	if e := tt.Click("清空选择"); e == nil && len(a.picked) != 2 {
		t.Fatal("busy changed selection")
	}
	a.busy = false
	a.query = "苹果"
	tt = ui.NewTester(a.view, 1000, 780)
	if tt.HasText("香蕉 · two · ") {
		t.Fatal("search")
	}
	tt.Click("清空选择")
	tt.Click("全选搜索结果")
	if len(a.picked) != 1 || !a.picked["a"] {
		t.Fatal(a.picked)
	}
}

func TestSourceChangeClearsSelection(t *testing.T) {
	a := &app{src: "old", scanned: "old", picked: map[string]bool{"a": true}, items: []core.Session{{Dir: "a"}}}
	a.changeSource("new")
	if a.scanned != "" || len(a.items) != 0 || len(a.picked) != 0 {
		t.Fatal("stale source state")
	}
}

func TestCancelAction(t *testing.T) {
	called := false
	a := &app{busy: true, picked: map[string]bool{}, cancel: func() { called = true }}
	tt := ui.NewTester(a.view, 1000, 850)
	if e := tt.Click("取消"); e != nil {
		t.Fatal(e)
	}
	if !called {
		t.Fatal("cancel not invoked")
	}
}

func TestRowsShowStateSizeTime(t *testing.T) {
	v := core.Session{Dir: "bad", ID: "invalid", Name: "错误录像", Bytes: 2 << 20, Error: "manifest 缺少字段"}
	v.Manifest.Time.Started = "2026-11-04T00:00:00Z"
	a := &app{picked: map[string]bool{}, items: []core.Session{v}}
	tt := ui.NewTester(a.view, 1000, 850)
	texts := strings.Join(tt.Texts(), "\n")
	for _, want := range []string{"2026-11-04", "2.0 MiB", "manifest 缺少字段"} {
		if !strings.Contains(texts, want) {
			t.Fatal(want, tt.Texts())
		}
	}
}

func TestLocalExportBadgeKeepsSelection(t *testing.T) {
	a := &app{picked: map[string]bool{}, items: []core.Session{{Dir: "a", ID: "one", Name: "已导出录像", LocalExportedAt: "2026-11-05T00:00:00Z", LocalExportPath: "/test/output"}}}
	tt := ui.NewTester(a.view, 1120, 760)
	if !tt.HasText("本机已导出") {
		t.Fatal(tt.Texts())
	}
	if err := tt.Click("全选搜索结果"); err != nil {
		t.Fatal(err)
	}
	if !a.picked["a"] {
		t.Fatal("exported recording must remain selectable")
	}
}

func TestRowClickToggles(t *testing.T) {
	a := &app{picked: map[string]bool{}, items: []core.Session{{Dir: "a", ID: "one", Name: "苹果"}}}
	tt := ui.NewTester(a.view, 1000, 780)
	if e := tt.Click("苹果"); e != nil {
		t.Fatal(e)
	}
	if !a.picked["a"] {
		t.Fatal("row click did not select")
	}
	if e := tt.Click("苹果 · one"); e != nil {
		t.Fatal(e)
	}
	if a.picked["a"] {
		t.Fatal("checkbox click did not deselect")
	}
}

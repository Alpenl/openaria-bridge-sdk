package main

import (
	"github.com/egoist/mygo/ui"
	"testing"
)

func TestExportWarningModal(t *testing.T) {
	p := &exportWarning{open: true, title: "视频未处理，已保存原始文件", detail: "未拼接、未转码；保存位置：D:/output/session-raw"}
	a := &app{picked: map[string]bool{}, exportWarning: p}
	tt := ui.NewTester(a.view, 1120, 760)
	if !tt.HasText(p.title) || !tt.HasText(p.detail) {
		t.Fatal(tt.Texts())
	}
	if err := tt.Click("知道了"); err != nil {
		t.Fatal(err)
	}
	if p.open {
		t.Fatal("warning did not close")
	}
}

package main

import (
	"testing"

	"github.com/egoist/mygo/ui"
)

func TestFolderSelectionSurvivesCardMonitor(t *testing.T) {
	a := &app{picked: map[string]bool{}, cardID: "old", cardChoice: "old"}
	a.selectFolder(t.TempDir())
	p := a.src
	if a.applyCards(cardDiscovery{Cards: []Card{{Path: "/dev/card", Label: "card", VolumeID: "id"}}}) || a.src != p || !a.folderSource {
		t.Fatal("card monitor replaced folder selection")
	}
	tt := ui.NewTester(a.view, 1000, 850)
	if !tt.HasText("选择录像文件夹") {
		t.Fatal(tt.Texts())
	}
	if a.src != p || !a.folderSource {
		t.Fatal("render replaced folder selection")
	}
}

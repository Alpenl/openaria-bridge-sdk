package main

import (
	"os"
	"path/filepath"
	"testing"
)

func TestSavedOutputBase(t *testing.T) {
	d := t.TempDir()
	cfg := filepath.Join(d, "config", "preferences.json")
	chosen := filepath.Join(d, "chosen")
	if e := savePreferences(cfg, preferences{chosen}); e != nil {
		t.Fatal(e)
	}
	p, e := loadPreferences(cfg)
	if e != nil || p.OutputBase != chosen {
		t.Fatalf("%+v %v", p, e)
	}
	if got := outputDirectory(p.OutputBase); got != filepath.Join(chosen, "OpenAriaExporter") {
		t.Fatal(got)
	}
	if _, e := os.Stat(chosen); !os.IsNotExist(e) {
		t.Fatal("saving setting should not create export tree")
	}
	next := filepath.Join(d, "next")
	if e = savePreferences(cfg, preferences{next}); e != nil {
		t.Fatal(e)
	}
	p, e = loadPreferences(cfg)
	if e != nil || p.OutputBase != next {
		t.Fatalf("replacement: %+v %v", p, e)
	}
}
func TestInvalidSavedOutput(t *testing.T) {
	p := filepath.Join(t.TempDir(), "settings")
	os.WriteFile(p, []byte(`{"output_base":"../relative"}`), 0600)
	v, e := loadPreferences(p)
	if e == nil || v.OutputBase == "../relative" {
		t.Fatalf("%+v %v", v, e)
	}
}

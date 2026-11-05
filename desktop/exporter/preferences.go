package main

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

type preferences struct {
	OutputBase string `json:"output_base"`
}

func preferencesPath() (string, error) {
	d, e := os.UserConfigDir()
	if e != nil {
		return "", e
	}
	return filepath.Join(d, "OpenAriaExporter", "preferences.json"), nil
}
func defaultOutputBase() string {
	h, e := os.UserHomeDir()
	if e != nil {
		return ""
	}
	d := filepath.Join(h, "Desktop")
	if i, e := os.Stat(d); e == nil && i.IsDir() {
		return d
	}
	return h
}
func loadPreferences(p string) (preferences, error) {
	v := preferences{OutputBase: defaultOutputBase()}
	b, e := os.ReadFile(p)
	if os.IsNotExist(e) {
		return v, nil
	}
	if e != nil {
		return v, e
	}
	var saved preferences
	if e = json.Unmarshal(b, &saved); e != nil {
		return v, e
	}
	if !filepath.IsAbs(saved.OutputBase) {
		return v, fmt.Errorf("保存位置必须为绝对路径")
	}
	return saved, nil
}
func savePreferences(p string, v preferences) error {
	if !filepath.IsAbs(v.OutputBase) {
		return fmt.Errorf("保存位置必须为绝对路径")
	}
	if e := os.MkdirAll(filepath.Dir(p), 0700); e != nil {
		return e
	}
	b, e := json.MarshalIndent(v, "", "  ")
	if e != nil {
		return e
	}
	f, e := os.CreateTemp(filepath.Dir(p), ".preferences-*")
	if e != nil {
		return e
	}
	defer os.Remove(f.Name())
	_, e = f.Write(b)
	if e == nil {
		e = f.Sync()
	}
	ce := f.Close()
	if e != nil {
		return e
	}
	if ce != nil {
		return ce
	}
	return os.Rename(f.Name(), p)
}
func outputDirectory(base string) string {
	if strings.TrimSpace(base) == "" {
		return ""
	}
	return filepath.Join(base, "OpenAriaExporter")
}
func (a *app) setOutputBase(p string) error {
	p, e := filepath.Abs(p)
	if e != nil {
		return e
	}
	cfg, e := preferencesPath()
	if e != nil {
		return e
	}
	if e = savePreferences(cfg, preferences{OutputBase: p}); e != nil {
		return e
	}
	a.out = p
	return nil
}

package main

import (
	"errors"
	"github.com/egoist/mygo"
	"io"
	"log"
	"openaria-exporter/internal/diagnostics"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"runtime/debug"
)

var userLog *diagnostics.Writer
var userLogPath string

// Attempt both sinks even when stderr is unavailable in a Windows GUI process.
type logSinks struct{ stderr, file io.Writer }

func (w logSinks) Write(p []byte) (int, error) {
	n, stderrErr := w.stderr.Write(p)
	if n != len(p) && stderrErr == nil {
		stderrErr = io.ErrShortWrite
	}
	m, fileErr := w.file.Write(p)
	if m != len(p) && fileErr == nil {
		fileErr = io.ErrShortWrite
	}
	return min(n, m), errors.Join(stderrErr, fileErr)
}

func setupLogging() {
	d, e := os.UserCacheDir()
	if e != nil {
		log.Printf("日志不可用: %v", e)
		return
	}
	userLogPath = filepath.Join(d, "OpenAriaExporter", "logs", "app.log")
	userLog, e = diagnostics.New(userLogPath)
	if e != nil {
		log.Printf("日志不可用: %v", e)
		return
	}
	log.SetOutput(logSinks{stderr: os.Stderr, file: userLog})
	log.SetFlags(log.Ldate | log.Ltime | log.Lmicroseconds)
	revision := "unknown"
	if info, ok := debug.ReadBuildInfo(); ok {
		for _, setting := range info.Settings {
			if setting.Key == "vcs.revision" {
				revision = setting.Value
			}
		}
	}
	log.Printf("stage=start version=%s revision=%s platform=%s/%s", mygo.App.Version(), revision, runtime.GOOS, runtime.GOARCH)
}
func logLocation() string {
	if userLog == nil {
		return "日志文件不可用；未保存详细日志"
	}
	return userLog.Location()
}
func (a *app) openLogs() {
	if userLog == nil {
		a.notice("日志不可用", logLocation())
		return
	}
	var cmd *exec.Cmd
	switch runtime.GOOS {
	case "darwin":
		cmd = exec.Command("/usr/bin/open", "-R", userLogPath)
	case "windows":
		cmd = exec.Command("explorer.exe", filepath.Dir(userLogPath))
	default:
		cmd = exec.Command("xdg-open", filepath.Dir(userLogPath))
	}
	if b, e := cmd.CombinedOutput(); e != nil {
		a.notice("无法打开日志", string(b)+e.Error()+"\n"+logLocation())
	}
}

package main

import (
	"bytes"
	"errors"
	"log"
	"openaria-exporter/internal/diagnostics"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

type unavailableStderr struct{}

func (unavailableStderr) Write([]byte) (int, error) {
	return 0, errors.New("invalid stderr handle")
}

func TestLogSinksStderrFailureStillPersists(t *testing.T) {
	path := filepath.Join(t.TempDir(), "app.log")
	file, err := diagnostics.New(path)
	if err != nil {
		t.Fatal(err)
	}
	log.New(logSinks{stderr: unavailableStderr{}, file: file}, "", 0).Print("diagnostic")
	b, err := os.ReadFile(path)
	if err != nil || string(b) != "diagnostic\n" {
		t.Fatalf("persistent log: %q, %v", b, err)
	}
	if file.Location() != "日志："+path {
		t.Fatal(file.Location())
	}

	// File failure must retain its own status while stderr still receives errors.
	if err := os.Remove(path); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(path, 0700); err != nil {
		t.Fatal(err)
	}
	var stderr bytes.Buffer
	log.New(logSinks{stderr: &stderr, file: file}, "", 0).Print("CLI error")
	if stderr.String() != "CLI error\n" || !strings.Contains(file.Location(), "失败") {
		t.Fatalf("stderr=%q location=%s", stderr.String(), file.Location())
	}
}

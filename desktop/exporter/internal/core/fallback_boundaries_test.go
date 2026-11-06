package core

import (
	"context"
	"crypto/sha256"
	"fmt"
	"io"
	"openaria-exporter/internal/source"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestRawPartialConflictPreservesOriginals(t *testing.T) {
	s, v := fixture(t, false)
	out := t.TempDir()
	stage := filepath.Join(out, "."+v.ID+"-raw.partial")
	if err := os.MkdirAll(filepath.Join(stage, "source"), 0700); err != nil {
		t.Fatal(err)
	}
	hash := sha256.Sum256(v.Raw)
	if err := os.WriteFile(filepath.Join(stage, ".claim"), []byte(fmt.Sprintf("%x\nraw", hash)), 0600); err != nil {
		t.Fatal(err)
	}
	saved := filepath.Join(stage, "source", "left.mp4")
	want := []byte("previous original must survive")
	if err := os.WriteFile(saved, want, 0600); err != nil {
		t.Fatal(err)
	}
	if _, err := exportOnce(t.Context(), s, v, Options{Output: out, rawOnly: true}); err == nil {
		t.Fatal("stale original overwritten")
	}
	got, err := os.ReadFile(saved)
	if err != nil || string(got) != string(want) {
		t.Fatalf("lost existing original %q %v", got, err)
	}
}

type unpluggedMediaSource struct{ source.Source }

func (s unpluggedMediaSource) Open(p string) (io.ReadCloser, error) {
	if strings.HasSuffix(p, ".mp4") || strings.HasSuffix(p, ".flac") {
		return nil, fmt.Errorf("simulated device disconnected")
	}
	return s.Source.Open(p)
}
func TestDisconnectedSourceDoesNotPublishRaw(t *testing.T) {
	requireMediaTools(t)
	s, v := fixture(t, false)
	out := t.TempDir()
	_, err := Export(t.Context(), unpluggedMediaSource{s}, v, Options{Output: out, VideoOnly: true, RawFallback: true})
	if err == nil || !strings.Contains(err.Error(), "disconnected") {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(out, v.ID+"-raw")); !os.IsNotExist(err) {
		t.Fatal("published disconnected raw", err)
	}
}

func TestHardwareFailureCacheScopedAndSelective(t *testing.T) {
	g := &mediaStream{}
	p := encoderProfile{stack: "hstack_qsv"}
	key := stackFailureKey(p, "h264", 1920, 1080)
	for _, err := range []error{context.Canceled, fmt.Errorf("Non-monotonic DTS"), fmt.Errorf("No space left on device")} {
		g.recordStackFailure(key, err)
		if g.stackFailed(key) {
			t.Fatal("unrelated error disabled hardware")
		}
	}
	g.recordStackFailure(key, fmt.Errorf("Could not create the texture (80070057)"))
	if !g.stackFailed(key) || g.stackFailed(stackFailureKey(p, "h264", 1280, 720)) {
		t.Fatal("hardware cache key not scoped")
	}
	if (&mediaStream{}).stackFailed(key) {
		t.Fatal("failure leaked into another batch")
	}
}

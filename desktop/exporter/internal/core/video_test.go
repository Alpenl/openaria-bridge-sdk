package core

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

func TestVideoOnlyOutputAndCleanup(t *testing.T) {
	s, v := fixture(t, true)
	t.Chdir(t.TempDir())
	out := "relative-output"
	p, e := Export(t.Context(), s, v, Options{Output: out, VideoOnly: true})
	if e != nil {
		t.Fatal(e)
	}
	for _, n := range []string{"work", "manifest.json", "receipt.json", "playback.mp4"} {
		if _, e = os.Stat(filepath.Join(p, n)); !os.IsNotExist(e) {
			t.Fatalf("retained %s", n)
		}
	}
	b, e := os.ReadFile(filepath.Join(p, "video.mp4"))
	if e != nil {
		t.Fatal(e)
	}
	size := len(b)
	b, e = os.ReadFile(filepath.Join(p, "export.json"))
	if e != nil {
		t.Fatal(e)
	}
	var record map[string]any
	if e = json.Unmarshal(b, &record); e != nil {
		t.Fatal(e)
	}
	if record["sha256"] != nil || record["media_hash_verified"] != false || record["bytes"] != float64(size) || record["source_copies_retained"] != true {
		t.Fatal(record)
	}
	info, e := probe(t.Context(), filepath.Join(p, "video.mp4"))
	if e != nil {
		t.Fatal(e)
	}
	if len(info.Streams) < 2 {
		t.Fatal("missing video/audio")
	}
	if _, e = Export(t.Context(), s, v, Options{Output: out, VideoOnly: true}); e == nil {
		t.Fatal("overwrite accepted")
	}
}
func TestVideoOnlyCancellationCleansAndRetries(t *testing.T) {
	s, v := fixture(t, true)
	out := t.TempDir()
	ctx, cancel := context.WithCancel(context.Background())
	_, e := Export(ctx, s, v, Options{Output: out, VideoOnly: true, Progress: func(p Progress) {
		if p.Stage == "边读边编码" {
			cancel()
		}
	}})
	if e == nil {
		t.Fatal("cancel ignored")
	}
	stage := filepath.Join(out, "."+v.ID+"-video.partial")
	for _, n := range []string{"source", "work"} {
		if _, e = os.Stat(filepath.Join(stage, n)); !os.IsNotExist(e) {
			t.Fatalf("temp retained %s %v", n, e)
		}
	}
	if _, e = os.Stat(filepath.Join(out, v.ID+"-video")); !os.IsNotExist(e) {
		t.Fatal("cancel published")
	}
	if _, e = Export(t.Context(), s, v, Options{Output: out, VideoOnly: true}); e != nil {
		t.Fatal(e)
	}
}

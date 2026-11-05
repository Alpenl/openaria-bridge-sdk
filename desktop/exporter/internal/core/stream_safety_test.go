package core

import (
	"context"
	"os"
	"path/filepath"
	"testing"
)

func TestStreamingRejectsStagingSymlink(t *testing.T) {
	requireMediaTools(t)
	s, v := fixture(t, false)
	out := t.TempDir()
	ctx, cancel := context.WithCancel(t.Context())
	cancel()
	if _, e := Export(ctx, s, v, Options{Output: out, VideoOnly: true}); e == nil {
		t.Fatal("cancellation ignored")
	}
	work := filepath.Join(out, "."+v.ID+"-video.partial", "work")
	if e := os.MkdirAll(work, 0700); e != nil {
		t.Fatal(e)
	}
	outside := filepath.Join(t.TempDir(), "keep.txt")
	if e := os.WriteFile(outside, []byte("preserve"), 0600); e != nil {
		t.Fatal(e)
	}
	if e := os.Symlink(outside, filepath.Join(work, "stream-0.txt")); e != nil {
		t.Skipf("symlinks unavailable: %v", e)
	}
	if _, e := Export(t.Context(), s, v, Options{Output: out, VideoOnly: true}); e == nil {
		t.Fatal("staging symlink accepted")
	}
	b, e := os.ReadFile(outside)
	if e != nil || string(b) != "preserve" {
		t.Fatalf("unrelated file changed: %q %v", b, e)
	}
}

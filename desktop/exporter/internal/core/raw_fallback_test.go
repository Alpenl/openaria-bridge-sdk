package core

import (
	"bytes"
	"context"
	"errors"
	"io"
	"os"
	"path"
	"path/filepath"
	"strings"
	"testing"
)

func TestRawFallbackPreservesBytesAndWarns(t *testing.T) {
	requireMediaTools(t)
	s, v := fixture(t, false) // Valid catalog, deliberately undecodable media.
	out := t.TempDir()
	dst, err := Export(t.Context(), s, v, Options{Output: out, VideoOnly: true, RawFallback: true})
	var warning *RawFallbackError
	if !errors.As(err, &warning) || dst != warning.Path || !strings.HasSuffix(dst, "（未合成）") {
		t.Fatalf("not an explicit raw-only result: %q %v", dst, err)
	}
	for _, a := range v.Artifacts {
		src, e := s.Open(path.Join(v.Dir, a.Path))
		if e != nil {
			t.Fatal(e)
		}
		want, e := io.ReadAll(src)
		src.Close()
		if e != nil {
			t.Fatal(e)
		}
		got, e := os.ReadFile(filepath.Join(dst, "source", a.Path))
		if e != nil || !bytes.Equal(got, want) {
			t.Fatalf("changed raw %s: %v", a.Path, e)
		}
	}
	for _, name := range []string{"source/manifest.json", "receipt.json", "UNPROCESSED.txt"} {
		if _, e := os.Stat(filepath.Join(dst, name)); e != nil {
			t.Fatal(e)
		}
	}
	for _, name := range []string{"video.mp4", "playback.mp4", "export.json", "work"} {
		if _, e := os.Stat(filepath.Join(dst, name)); !os.IsNotExist(e) {
			t.Fatalf("false product %s: %v", name, e)
		}
	}
	// A repeated attempt must never overwrite a saved raw export.
	if _, err = Export(t.Context(), s, v, Options{Output: out, VideoOnly: true, RawFallback: true}); errors.As(err, &warning) || err == nil {
		t.Fatalf("overwrote raw output: %v", err)
	}
}
func TestRawFallbackBatchContinuesWithoutVideoMarks(t *testing.T) {
	requireMediaTools(t)
	s, v := fixture(t, false)
	other := v
	other.ID = "second-session"
	completed := 0
	paths, err := ExportBatch(t.Context(), s, []Session{v, other}, Options{Output: t.TempDir(), RawFallback: true}, nil, func(int, string) { completed++ })
	var warning *RawFallbackError
	if !errors.As(err, &warning) || len(paths) != 2 || paths[0] == "" || paths[1] == "" || completed != 0 {
		t.Fatalf("raw treated as video success or batch stopped: %v %v complete=%d", paths, err, completed)
	}
}
func TestRawFallbackCancellationDoesNotPublish(t *testing.T) {
	requireMediaTools(t)
	for _, duringCopy := range []bool{false, true} {
		s, v := fixture(t, false)
		out := t.TempDir()
		ctx, cancel := context.WithCancel(t.Context())
		if !duringCopy {
			cancel()
		}
		_, err := Export(ctx, s, v, Options{Output: out, VideoOnly: true, RawFallback: true, Progress: func(p Progress) {
			if p.Stage == "处理失败，保存原始文件" {
				cancel()
			}
		}})
		cancel()
		var warning *RawFallbackError
		if err == nil || errors.As(err, &warning) {
			t.Fatalf("cancel reported saved raw: %v", err)
		}
		if _, e := os.Stat(filepath.Join(out, recordingName(v, "raw", 1))); !os.IsNotExist(e) {
			t.Fatal("published canceled raw", e)
		}
		if _, e := os.Stat(filepath.Join(out, "."+v.ID+"-raw.partial", "source")); !os.IsNotExist(e) {
			t.Fatal("retained canceled source copies", e)
		}
	}
}
func TestRawFallbackDoesNotRunOnOutputConflict(t *testing.T) {
	requireMediaTools(t)
	s, v := fixture(t, false)
	out := t.TempDir()
	root, err := os.OpenRoot(out)
	if err != nil {
		t.Fatal(err)
	}
	defer root.Close()
	lock, err := managedLock(root, "session/"+v.Manifest.VolumeID+"/"+v.ID+"/video")
	if err != nil {
		t.Fatal(err)
	}
	defer lock.Close()
	_, err = Export(t.Context(), s, v, Options{Output: out, VideoOnly: true, RawFallback: true})
	if err == nil {
		t.Fatal("expected conflict")
	}
	if _, e := os.Stat(filepath.Join(out, recordingName(v, "raw", 1))); !os.IsNotExist(e) {
		t.Fatal("unexpected fallback", e)
	}
}

package core

import (
	"context"
	"errors"
	"io"
	"openaria-exporter/internal/source"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

func nameFixture(v Session) Session {
	v.RecordingTime = RecordingTime{StartedAt: "2026-11-09T17:30:28.426651+08:00", Status: "recorded", Source: "unknown"}
	return v
}

type noReadsSource struct{ source.Source }

func (s noReadsSource) Open(string) (io.ReadCloser, error) {
	return nil, errors.New("unexpected source read")
}

func TestReadableNamesCollisionReuseAndDeletion(t *testing.T) {
	s, v, _ := completeFixture(t, true)
	v = nameFixture(v)
	other := v
	other.ID = "second"
	out := t.TempDir()
	paths, err := ExportBatch(t.Context(), s, []Session{v, other}, Options{Output: out, batchTestWorkers: 2}, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	want := map[string]bool{"2026-11-09 17时30分28秒": true, "2026-11-09 17时30分28秒（2）": true}
	for _, p := range paths {
		if !want[filepath.Base(p)] {
			t.Fatal(p)
		}
		delete(want, filepath.Base(p))
	}
	// The output carries recording creation_time in UTC; original fractional
	// seconds and timezone remain in export.json regardless of MP4 precision.
	b, err := exec.Command("ffprobe", "-v", "error", "-show_entries", "format_tags=creation_time", "-of", "default=nw=1:nk=1", filepath.Join(paths[0], "video.mp4")).Output()
	if err != nil || !strings.Contains(string(b), "2026-11-09T09:30:28") {
		t.Fatalf("creation time %s %v", b, err)
	}
	for i, vv := range []Session{v, other} {
		again, err := Export(t.Context(), noReadsSource{s}, vv, Options{Output: out, VideoOnly: true})
		if err != nil || again != paths[i] {
			t.Fatalf("duplicate export %q %v", again, err)
		}
	}
	// Delete the lower number, then verify the other is found past the gap.
	first := 0
	if strings.Contains(filepath.Base(paths[0]), "（2）") {
		first = 1
	}
	vv := []Session{v, other}
	receipt, err := InspectLocalOutput(paths[first], vv[first])
	if err != nil {
		t.Fatal(err)
	}
	root, err := os.OpenRoot(out)
	if err != nil {
		t.Fatal(err)
	}
	defer root.Close()
	held, err := managedLock(root, "output/"+filepath.Base(paths[first]))
	if err != nil {
		t.Fatal(err)
	}
	if err = DeleteLocalOutput(receipt); err == nil {
		t.Fatal("delete ignored name reservation")
	}
	held.Close()
	if err = DeleteLocalOutput(receipt); err != nil {
		t.Fatal(err)
	}
	survivor := 1 - first
	again, err := Export(t.Context(), noReadsSource{s}, vv[survivor], Options{Output: out, VideoOnly: true})
	if err != nil || again != paths[survivor] {
		t.Fatal("hole duplicated recording", again, err)
	}
	entries, err := os.ReadDir(out)
	if err != nil {
		t.Fatal(err)
	}
	for _, e := range entries {
		if strings.HasSuffix(e.Name(), ".lock") {
			t.Fatal("lock scattered in output root", e.Name())
		}
	}
}
func TestReadableNamesLegacyAndDamagedOutput(t *testing.T) {
	s, v := fixture(t, true)
	v = nameFixture(v)
	out := t.TempDir()
	p, err := Export(t.Context(), s, v, Options{Output: out, VideoOnly: true})
	if err != nil {
		t.Fatal(err)
	}
	legacy := filepath.Join(out, v.ID+"-video")
	if err = os.Rename(p, legacy); err != nil {
		t.Fatal(err)
	}
	p, err = Export(t.Context(), noReadsSource{s}, v, Options{Output: out, VideoOnly: true})
	if err != nil || p != legacy {
		t.Fatal(p, err)
	}
	if err = os.Remove(filepath.Join(legacy, "source", "audio.flac")); err != nil {
		t.Fatal(err)
	}
	if _, err = Export(t.Context(), s, v, Options{Output: out, VideoOnly: true, RawFallback: true}); err == nil {
		t.Fatal("damaged output silently reused")
	}
}
func TestReadableNamesReserveUnknownAndCancellation(t *testing.T) {
	out := t.TempDir()
	root, err := os.OpenRoot(out)
	if err != nil {
		t.Fatal(err)
	}
	defer root.Close()
	v := Session{ID: "one"}
	name, lock, existing, err := reserveRecordingName(t.Context(), root, v, "video")
	if err != nil || existing || name != "录制时间未知（1）" {
		t.Fatal(name, err)
	}
	defer lock.Close()
	other := Session{ID: "two"}
	name, second, _, err := reserveRecordingName(t.Context(), root, other, "video")
	if err != nil || name != "录制时间未知（2）" {
		t.Fatal(name, err)
	}
	second.Close()
	ctx, cancel := context.WithCancel(t.Context())
	cancel()
	if _, _, _, err = reserveRecordingName(ctx, root, other, "video"); !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
}
func TestReadableNameConflictPreservesUserFolder(t *testing.T) {
	s, v := fixture(t, true)
	v = nameFixture(v)
	out := t.TempDir()
	user := filepath.Join(out, recordingName(v, "video", 1))
	if err := os.Mkdir(user, 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(user, "notes.txt"), []byte("keep"), 0600); err != nil {
		t.Fatal(err)
	}
	p, err := Export(t.Context(), s, v, Options{Output: out, VideoOnly: true})
	if err != nil || filepath.Base(p) != recordingName(v, "video", 2) {
		t.Fatal(p, err)
	}
	if b, err := os.ReadFile(filepath.Join(user, "notes.txt")); err != nil || string(b) != "keep" {
		t.Fatal("lost user folder", err)
	}
}

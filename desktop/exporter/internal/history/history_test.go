package history

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"sync"
	"testing"

	"openaria-exporter/internal/core"
)

func fixture(t *testing.T, dir string) (core.Session, string) {
	t.Helper()
	v := core.Session{ID: "session-1", Raw: []byte(`{"fixture":true}`)}
	v.Manifest.VolumeID = "volume-1"
	v.Manifest.Video.Segments = []core.Segment{{Index: 0, End: 1}}
	p := filepath.Join(dir, "session-1-video")
	if e := os.MkdirAll(p, 0700); e != nil {
		t.Fatal(e)
	}
	h := sha256.Sum256(v.Raw)
	b, _ := json.Marshal(map[string]any{"session_id": v.ID, "source_manifest_sha256": hex.EncodeToString(h[:]), "file": "video.mp4", "bytes": 5})
	for n, b := range map[string][]byte{"video.mp4": []byte("video"), "export.json": b, ".claim": []byte("claim"), ".lock": nil} {
		if e := os.WriteFile(filepath.Join(p, n), b, 0600); e != nil {
			t.Fatal(e)
		}
	}
	return v, p
}
func TestMarksRestartIsolationRevisionAndDeletion(t *testing.T) {
	base := t.TempDir()
	v, p := fixture(t, base)
	s := Store{filepath.Join(base, "client-mac", "history #中文%.sqlite3")}
	mark, e := s.Record(v, p)
	if e != nil {
		t.Fatal(e)
	}
	reopened := Store{s.Path}
	items := []core.Session{v}
	if e = reopened.Apply(items); e != nil || items[0].LocalExportedAt != mark.ExportedAt {
		t.Fatal(items, e)
	}
	other := Store{filepath.Join(base, "client-windows", "history.sqlite3")}
	if e = other.Apply(items); e != nil || items[0].LocalExportedAt != "" {
		t.Fatal("client state leaked", e)
	}
	renamed := v
	renamed.Name = "new name"
	renamed.Manifest.Name = "new name"
	renamed.Dir = "different-drive"
	items = []core.Session{renamed}
	if e = s.Apply(items); e != nil || items[0].LocalExportedAt == "" {
		t.Fatal("rename lost mark", e)
	}
	otherVolume := v
	otherVolume.Manifest.VolumeID = "volume-2"
	changed := v
	changed.Manifest.Video.Segments = append([]core.Segment(nil), v.Manifest.Video.Segments...)
	changed.Manifest.Video.Segments[0].End = 2
	items = []core.Session{otherVolume, changed}
	if e = s.Apply(items); e != nil {
		t.Fatal(e)
	}
	for _, item := range items {
		if item.LocalExportedAt != "" {
			t.Fatal("wrong identity marked")
		}
	}
	if e = s.Clear([]core.Session{v}); e != nil {
		t.Fatal(e)
	}
	outputs, e := s.Outputs([]core.Session{v})
	if e != nil || len(outputs) != 1 {
		t.Fatal("clear lost output receipt", outputs, e)
	}
	if _, e = os.Stat(filepath.Join(p, "video.mp4")); e != nil {
		t.Fatal("clear deleted video", e)
	}
	if _, e = s.Record(v, p); e != nil {
		t.Fatal(e)
	}
	if e = core.DeleteLocalOutput(outputs[0].Receipt); e != nil {
		t.Fatal(e)
	}
	if _, e = os.Stat(p); !os.IsNotExist(e) {
		t.Fatal("directory remains", e)
	}
	items = []core.Session{v}
	if e = s.Apply(items); e != nil || items[0].LocalExportedAt == "" {
		t.Fatal("delete cleared mark", e)
	}
	if e = s.ForgetOutput(outputs[0].ID); e != nil {
		t.Fatal(e)
	}
	outputs, e = s.Outputs([]core.Session{v})
	if e != nil || len(outputs) != 0 {
		t.Fatal(outputs, e)
	}
}
func TestRejectChangedExtraAndLinkedOutput(t *testing.T) {
	for _, kind := range []string{"extra", "changed", "symlink", "receipt"} {
		t.Run(kind, func(t *testing.T) {
			v, p := fixture(t, t.TempDir())
			out, e := core.InspectLocalOutput(p, v)
			if e != nil {
				t.Fatal(e)
			}
			switch kind {
			case "extra":
				e = os.WriteFile(filepath.Join(p, "my-notes.txt"), []byte("keep"), 0600)
			case "changed":
				e = os.WriteFile(filepath.Join(p, "video.mp4"), []byte("replacement video"), 0600)
			case "receipt":
				e = os.WriteFile(filepath.Join(p, "export.json"), []byte("{}"), 0600)
			case "symlink":
				real := p + "-moved"
				if e = os.Rename(p, real); e != nil {
					t.Fatal(e)
				}
				if e = os.Symlink(real, p); e != nil {
					t.Skip("symlinks unavailable", e)
				}
			}
			if e != nil {
				t.Fatal(e)
			}
			if e = core.DeleteLocalOutput(out); e == nil {
				t.Fatal("unsafe output deleted")
			}
			if _, e = os.Stat(filepath.Join(p, "video.mp4")); e != nil {
				t.Fatal("video was removed", e)
			}
		})
	}
}
func TestNoMarkForUnpublishedOrInvalidOutput(t *testing.T) {
	base := t.TempDir()
	v, p := fixture(t, base)
	s := Store{filepath.Join(base, "history.sqlite3")}
	if _, e := s.Record(v, p+"-missing"); e == nil {
		t.Fatal("missing output recorded")
	}
	wrong := v
	wrong.ID = "different"
	if _, e := s.Record(wrong, p); e == nil {
		t.Fatal("wrong session recorded")
	}
	items := []core.Session{v}
	if e := s.Apply(items); e != nil || items[0].LocalExportedAt != "" {
		t.Fatal(items, e)
	}
}
func TestConcurrentClientsAndCorruptStore(t *testing.T) {
	base := t.TempDir()
	v, p := fixture(t, base)
	s := Store{filepath.Join(base, "history.sqlite3")}
	var wg sync.WaitGroup
	for i := 0; i < 6; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if _, e := s.Record(v, p); e != nil {
				t.Error(e)
			}
		}()
	}
	wg.Wait()
	out, e := s.Outputs([]core.Session{v})
	if e != nil || len(out) != 1 {
		t.Fatal(out, e)
	}
	bad := Store{filepath.Join(base, "broken.sqlite3")}
	if e = os.WriteFile(bad.Path, []byte("broken database"), 0600); e != nil {
		t.Fatal(e)
	}
	if e = bad.Apply([]core.Session{v}); e == nil {
		t.Fatal("corruption not surfaced")
	}
}

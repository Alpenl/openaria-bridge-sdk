package core

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

func TestLocalDeleteSharesExportLock(t *testing.T) {
	base := t.TempDir()
	p := filepath.Join(base, "test-video")
	if err := os.Mkdir(p, 0700); err != nil {
		t.Fatal(err)
	}
	v := Session{ID: "test", Raw: []byte("fixture")}
	h := sha256.Sum256(v.Raw)
	b, _ := json.Marshal(map[string]any{"session_id": "test", "source_manifest_sha256": hex.EncodeToString(h[:]), "file": "video.mp4", "bytes": 5})
	if err := os.WriteFile(filepath.Join(p, "video.mp4"), []byte("video"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(p, "export.json"), b, 0600); err != nil {
		t.Fatal(err)
	}
	out, err := InspectLocalOutput(p, v)
	if err != nil {
		t.Fatal(err)
	}
	lock, err := os.OpenFile(filepath.Join(base, ".test-video.lock"), os.O_CREATE|os.O_RDWR, 0600)
	if err != nil {
		t.Fatal(err)
	}
	defer lock.Close()
	if err = lockFile(lock); err != nil {
		t.Fatal(err)
	}
	if err = DeleteLocalOutput(out); err == nil {
		t.Fatal("deleted a locked output")
	}
	if _, err = os.Stat(filepath.Join(p, "video.mp4")); err != nil {
		t.Fatal(err)
	}
	lock.Close()
	if err = DeleteLocalOutput(out); err != nil {
		t.Fatal(err)
	}
}

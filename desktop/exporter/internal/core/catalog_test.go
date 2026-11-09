package core

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"openaria-exporter/internal/source"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestCatalogWALReadOnlyAndVolumeBinding(t *testing.T) {
	d := t.TempDir()
	p := filepath.Join(d, "catalog.sqlite3")
	db, e := sql.Open("sqlite", p)
	if e != nil {
		t.Fatal(e)
	}
	defer db.Close()
	db.SetMaxOpenConns(1)
	for _, stmt := range []string{"PRAGMA journal_mode=WAL", "PRAGMA wal_autocheckpoint=0", "PRAGMA application_id=1498175489", "PRAGMA user_version=1", "CREATE TABLE sessions(volume TEXT,session TEXT,signature TEXT,summary TEXT)"} {
		if _, e = db.Exec(stmt); e != nil {
			t.Fatal(e)
		}
	}
	raw := []byte(`{"current":"manifest"}`)
	h := sha256.Sum256(raw)
	sig, _ := json.Marshal([]any{1, 2, hex.EncodeToString(h[:])})
	summary := `{"session_id":"same-id","display_name":"SQLite 名称","started_at":"2026-11-04T10:00:00Z","duration_seconds":2,"take_id":"take","take_sequence":2}`
	if _, e = db.Exec("INSERT INTO sessions VALUES(?,?,?,?)", "volume-a", "same-id", string(sig), summary); e != nil {
		t.Fatal(e)
	}
	// Leave the connection open: committed schema and row remain in WAL.
	original := map[string][32]byte{}
	for _, suffix := range []string{"", "-wal"} {
		b, e := os.ReadFile(p + suffix)
		if e != nil {
			t.Fatal(e)
		}
		original[suffix] = sha256.Sum256(b)
	}
	s, e := source.NewFolder(d)
	if e != nil {
		t.Fatal(e)
	}
	defer s.Close()
	items := []Session{{ID: "same-id", Name: "manifest name", Raw: raw, Manifest: Manifest{VolumeID: "volume-a"}}, {ID: "same-id", Name: "other volume", Raw: raw, Manifest: Manifest{VolumeID: "volume-b"}}}
	if e = enrichCatalog(context.Background(), s, items); e != nil {
		t.Fatal(e)
	}
	if items[0].Name != "SQLite 名称" || !strings.HasPrefix(items[0].MetadataSource, "sqlite:") || items[0].TakeSequence != 2 {
		t.Fatalf("not hydrated %+v", items[0])
	}
	if items[0].Manifest.Time.Started != "" || items[0].Manifest.Time.Duration != 0 || items[0].MetadataWarning == "" {
		t.Fatal("catalog replaced original recording time", items[0])
	}
	if items[1].Name != "other volume" {
		t.Fatal("cross-volume contamination")
	}
	for suffix, want := range original {
		b, e := os.ReadFile(p + suffix)
		if e != nil || sha256.Sum256(b) != want {
			t.Fatalf("source changed %s %v", suffix, e)
		}
	}
	items[0].Raw = []byte("changed")
	items[0].Name = "manifest fallback"
	items[0].MetadataSource = "manifest"
	if e = enrichCatalog(context.Background(), s, items); e != nil {
		t.Fatal(e)
	}
	if items[0].Name != "manifest fallback" || items[0].MetadataWarning == "" {
		t.Fatal("stale cache accepted")
	}
}
func TestCatalogCorruptIsVisible(t *testing.T) {
	d := t.TempDir()
	os.WriteFile(filepath.Join(d, "catalog.sqlite3"), []byte("broken"), 0600)
	s, e := source.NewFolder(d)
	if e != nil {
		t.Fatal(e)
	}
	defer s.Close()
	if e = enrichCatalog(t.Context(), s, nil); e == nil {
		t.Fatal("corruption silently hidden")
	}
}

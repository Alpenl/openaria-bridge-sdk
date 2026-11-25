package core

import (
	"os"
	"path/filepath"
	"testing"

	"openaria-exporter/internal/source"
)

func TestCopiedRecordingFolderLevels(t *testing.T) {
	requireMediaTools(t)
	_, original, files := completeFixture(t, true)
	root := t.TempDir()
	recording := filepath.Join(root, "recordings", "2026-11-25", "14-32-08")
	for p, data := range files {
		name := filepath.Join(recording, p)
		if err := os.MkdirAll(filepath.Dir(name), 0700); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(name, data, 0600); err != nil {
			t.Fatal(err)
		}
	}
	// Folder import must not depend on a copied device database, even a broken one.
	if err := os.WriteFile(filepath.Join(root, "catalog.sqlite3"), []byte("not a database"), 0600); err != nil {
		t.Fatal(err)
	}
	// A volume-root selection must ignore unrelated system folders.
	if err := os.MkdirAll(filepath.Join(root, "System Volume Information"), 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "System Volume Information", "manifest.json"), []byte("invalid"), 0600); err != nil {
		t.Fatal(err)
	}
	for _, selected := range []string{root, filepath.Join(root, "recordings"), filepath.Dir(recording), recording} {
		t.Run(filepath.Base(selected), func(t *testing.T) {
			s, err := source.NewFolder(selected)
			if err != nil {
				t.Fatal(err)
			}
			defer s.Close()
			items, err := ScanFolder(t.Context(), s)
			if err != nil || len(items) != 1 || items[0].ID != original.ID || items[0].Error != "" {
				t.Fatalf("scan: %v %+v", err, items)
			}
			if _, err = Export(t.Context(), s, items[0], Options{Output: t.TempDir(), VideoOnly: true}); err != nil {
				t.Fatal(err)
			}
			if _, err = Export(t.Context(), s, items[0], Options{Output: filepath.Join(recording, "output"), VideoOnly: true}); err == nil {
				t.Fatal("allowed output within recording source")
			}
		})
	}
}

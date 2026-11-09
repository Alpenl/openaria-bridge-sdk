package core

import (
	"os"
	"path/filepath"
	"testing"
)

func TestEmptyStageCleanupPreservesUnexpectedContents(t *testing.T) {
	for _, extra := range []string{"", "notes.txt", "video.mp4", "source"} {
		t.Run(extra, func(t *testing.T) {
			dir := t.TempDir()
			root, err := os.OpenRoot(dir)
			if err != nil {
				t.Fatal(err)
			}
			defer root.Close()
			if err := root.Mkdir("stage", 0700); err != nil {
				t.Fatal(err)
			}
			st, err := root.OpenRoot("stage")
			if err != nil {
				t.Fatal(err)
			}
			defer st.Close()
			if err := writeNew(st, ".claim", []byte("claim")); err != nil {
				t.Fatal(err)
			}
			if err := writeNew(st, ".lock", nil); err != nil {
				t.Fatal(err)
			}
			if extra == "source" {
				err = st.Mkdir(extra, 0700)
			} else if extra != "" {
				err = writeNew(st, extra, []byte("keep"))
			}
			if err != nil {
				t.Fatal(err)
			}
			if err := removeEmptyStage(root, st, "stage", "claim"); err != nil {
				t.Fatal(err)
			}
			_, err = os.Stat(filepath.Join(dir, "stage"))
			if extra == "" && !os.IsNotExist(err) {
				t.Fatal("empty shell retained", err)
			}
			if extra != "" {
				if err != nil {
					t.Fatal("unexpected content removed", err)
				}
				if _, err := st.Stat(extra); err != nil {
					t.Fatal(err)
				}
			}
		})
	}
}

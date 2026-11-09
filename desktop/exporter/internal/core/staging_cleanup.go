package core

import (
	"os"
)

// Only remove a newly created, empty staging shell after its child handles and
// lock have closed. The caller still holds the parent session/output locks.
func removeEmptyStage(root, st *os.Root, stage, claim string) error {
	f, err := st.Open(".")
	if err != nil {
		return err
	}
	entries, err := f.ReadDir(-1)
	ce := f.Close()
	if err != nil {
		return err
	}
	if ce != nil {
		return ce
	}
	for _, entry := range entries {
		if (entry.Name() != ".claim" && entry.Name() != ".lock") || !entry.Type().IsRegular() {
			return nil // Preserve unexpected files and incomplete products for inspection.
		}
	}
	if b, err := st.ReadFile(".claim"); err != nil || string(b) != claim {
		return nil
	}
	for _, name := range []string{".lock", ".claim"} {
		if err := st.Remove(name); err != nil && !os.IsNotExist(err) {
			return err
		}
	}
	if err := st.Close(); err != nil {
		return err
	}
	return root.Remove(stage) // Fails safely if anything appeared in the directory.
}

package diagnostics

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestRotationAndFailure(t *testing.T) {
	p := filepath.Join(t.TempDir(), "app.log")
	w, e := New(p)
	if e != nil {
		t.Fatal(e)
	}
	for i := 0; i < 3; i++ {
		if _, e = w.Write([]byte(strings.Repeat("x", Limit))); e != nil {
			t.Fatal(e)
		}
	}
	for _, name := range []string{p, p + ".1"} {
		s, e := os.Stat(name)
		if e != nil || s.Size() > Limit {
			t.Fatalf("%s %v", name, e)
		}
	}
	os.Remove(p)
	os.Mkdir(p, 0700)
	if _, e = w.Write([]byte("fail")); e == nil {
		t.Fatal("expected failure")
	}
	if !strings.Contains(w.Location(), "失败") {
		t.Fatal(w.Location())
	}
}

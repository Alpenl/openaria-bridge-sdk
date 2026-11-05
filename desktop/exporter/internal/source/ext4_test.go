package source

import (
	"bytes"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"testing"
)

func mk(t *testing.T, p, root, size string) {
	t.Helper()
	bin, err := exec.LookPath("mke2fs")
	if err != nil {
		bin = "/opt/homebrew/opt/e2fsprogs/sbin/mke2fs"
		if _, e := os.Stat(bin); e != nil {
			t.Skip("需要 e2fsprogs")
		}
	}
	b, e := exec.Command(bin, "-q", "-t", "ext4", "-F", "-O", "^metadata_csum_seed", "-d", root, p, size).CombinedOutput()
	if e != nil {
		t.Fatalf("mke2fs %v %s", e, b)
	}
}
func TestNestedSparseAndLarge(t *testing.T) {
	d := t.TempDir()
	innerRoot := filepath.Join(d, "inner")
	os.MkdirAll(filepath.Join(innerRoot, "recordings"), 0755)
	f, e := os.Create(filepath.Join(innerRoot, "recordings", "sparse"))
	if e != nil {
		t.Fatal(e)
	}
	offset := int64(4<<30) + 12345
	f.WriteAt([]byte("large-offset"), offset)
	f.Close()
	inner := filepath.Join(d, "inner.img")
	mk(t, inner, innerRoot, "6G")
	outerRoot := filepath.Join(d, "outer")
	target := filepath.Join(outerRoot, "var/lib/rp-ylx-data-volume")
	os.MkdirAll(target, 0755)
	if e = os.Rename(inner, filepath.Join(target, "data-volume.img")); e != nil {
		t.Fatal(e)
	}
	outer := filepath.Join(d, "outer.img")
	mk(t, outer, outerRoot, "7G")
	s, e := Open(outer)
	if e != nil {
		t.Fatal(e)
	}
	defer s.Close()
	x := s.(*Ext4)
	n, e := x.node("recordings/sparse")
	if e != nil {
		t.Fatal(e)
	}
	b := make([]byte, 12)
	if _, e = n.ReadAt(b, offset); e != nil {
		t.Fatal(e)
	}
	if string(b) != "large-offset" {
		t.Fatalf("%q", b)
	}
	clear(b)
	if _, e = n.ReadAt(b, 3<<30); e != nil {
		t.Fatal(e)
	}
	if !bytes.Equal(b, make([]byte, 12)) {
		t.Fatal("hole")
	}
	if _, e = s.Open("../escape"); e == nil {
		t.Fatal("escape accepted")
	}
}
func TestFolderSymlink(t *testing.T) {
	d := t.TempDir()
	os.WriteFile(filepath.Join(d, "ok"), []byte("ok"), 0600)
	os.Symlink("ok", filepath.Join(d, "link"))
	s, e := NewFolder(d)
	if e != nil {
		t.Fatal(e)
	}
	defer s.Close()
	if _, e = s.Open("link"); e == nil {
		t.Fatal("symlink")
	}
	r, e := s.Open("ok")
	if e != nil {
		t.Fatal(e)
	}
	defer r.Close()
	b, _ := io.ReadAll(r)
	if string(b) != "ok" {
		t.Fatal("read")
	}
}

type countingReader struct{ calls, max int }

func (c *countingReader) ReadAt(b []byte, off int64) (int, error) {
	c.calls++
	if len(b) > c.max {
		c.max = len(b)
	}
	clear(b)
	return len(b), nil
}
func TestCoalescedRuns(t *testing.T) {
	r := &countingReader{}
	x := &Ext4{r: r, bs: 4096}
	n := &inode{x: x, size: 32 << 20, ext: []extent{{0, 100, 8192, false}}}
	b := make([]byte, 1<<20)
	for off := int64(0); off < n.size; off += int64(len(b)) {
		if _, e := n.ReadAt(b, off); e != nil {
			t.Fatal(e)
		}
	}
	if r.calls != 32 || r.max != 1<<20 {
		t.Fatalf("calls=%d max=%d", r.calls, r.max)
	}
	n.ext = []extent{{4096, 0, 4096, true}}
	r.calls = 0
	for off := int64(0); off < n.size; off += int64(len(b)) {
		n.ReadAt(b, off)
	}
	if r.calls != 0 {
		t.Fatal("holes/unwritten caused I/O")
	}
}

func TestAlignedReader(t *testing.T) {
	r := bytes.NewReader(bytes.Repeat([]byte("0123456789abcdef"), 1024))
	a := AlignedReader{r, 512}
	b := make([]byte, 700)
	if _, e := a.ReadAt(b, 123); e != nil {
		t.Fatal(e)
	}
	want := make([]byte, 700)
	r.ReadAt(want, 123)
	if !bytes.Equal(b, want) {
		t.Fatal("aligned offset")
	}
}

func TestOutputContainmentIdentity(t *testing.T) {
	base := t.TempDir()
	root := filepath.Join(base, "Source")
	os.Mkdir(root, 0700)
	s, e := NewFolder(root)
	if e != nil {
		t.Fatal(e)
	}
	defer s.Close()
	alias := filepath.Join(base, "alias")
	os.Symlink(root, alias)
	for _, p := range []string{root, filepath.Join(root, "nested", "exports"), filepath.Join(alias, "new", "exports")} {
		if ValidateOutput(s, p) == nil {
			t.Fatalf("accepted %s", p)
		}
		if _, e = os.Stat(p); p != root && !os.IsNotExist(e) {
			t.Fatal("check created output")
		}
	}
	if ValidateOutput(s, filepath.Join(base, "other")) != nil {
		t.Fatal("outside rejected")
	}
	if info, e := os.Stat(filepath.Join(base, "source")); e == nil {
		orig, _ := os.Stat(root)
		if os.SameFile(orig, info) && ValidateOutput(s, filepath.Join(base, "source", "exports")) == nil {
			t.Fatal("case alias accepted")
		}
	}
}
func TestJournalWarnings(t *testing.T) {
	if Warnings(&Ext4{}) != "" {
		t.Fatal("clean warning")
	}
	if Warnings(&Ext4{OuterJournalPending: true}) == "" {
		t.Fatal("pending warning missing")
	}
}

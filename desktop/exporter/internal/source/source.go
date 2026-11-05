package source

import (
	"fmt"
	"io"
	"io/fs"
	"os"
	"path"
	"path/filepath"
	"strings"
)

type Entry struct {
	Name string
	Size int64
	Dir  bool
}
type Source interface {
	List(string) ([]Entry, error)
	Open(string) (io.ReadCloser, error)
	Close() error
}

func Safe(p string) error {
	if p == "." {
		return nil
	}
	if p == "" || strings.ContainsAny(p, "\\:\x00\r\n*?<>|\"") || strings.HasPrefix(p, "/") || path.Clean(p) != p || p == ".." || strings.HasPrefix(p, "../") {
		return fmt.Errorf("不安全的路径 %q", p)
	}
	for _, v := range strings.Split(p, "/") {
		upper := strings.ToUpper(strings.Split(v, ".")[0])
		if strings.TrimRight(v, " .") != v || upper == "CON" || upper == "PRN" || upper == "AUX" || upper == "NUL" || strings.HasPrefix(upper, "COM") && len(upper) == 4 || strings.HasPrefix(upper, "LPT") && len(upper) == 4 {
			return fmt.Errorf("不支持跨平台文件名 %q", v)
		}
	}
	return nil
}

type Folder struct {
	root     *os.Root
	identity os.FileInfo
}

func NewFolder(p string) (Source, error) {
	r, e := os.OpenRoot(p)
	if e != nil {
		return nil, e
	}
	f, e := r.Open(".")
	if e != nil {
		r.Close()
		return nil, e
	}
	info, e := f.Stat()
	f.Close()
	if e != nil {
		r.Close()
		return nil, e
	}
	return &Folder{r, info}, nil
}

// Compare directory identities, not spelling: existing ancestors resolve symlinks,
// case aliases and renamed roots. No directories are created by this check.
func ValidateOutput(s Source, p string) error {
	f, ok := s.(*Folder)
	if !ok {
		return nil
	}
	abs, e := filepath.Abs(p)
	if e != nil {
		return e
	}
	for {
		_, err := os.Stat(abs)
		if err == nil {
			resolved, e := filepath.EvalSymlinks(abs)
			if e != nil {
				return e
			}
			for cur := resolved; ; cur = filepath.Dir(cur) {
				i, e := os.Stat(cur)
				if e != nil {
					return e
				}
				if os.SameFile(f.identity, i) {
					return fmt.Errorf("输出目录不能位于源文件夹内，请选择源文件夹之外的位置")
				}
				if filepath.Dir(cur) == cur {
					break
				}
			}
			return nil
		}
		if !os.IsNotExist(err) {
			return err
		}
		parent := filepath.Dir(abs)
		if parent == abs {
			return err
		}
		abs = parent
	}
}

func (f *Folder) check(p string) error {
	if e := Safe(p); e != nil {
		return e
	}
	cur := ""
	for _, v := range strings.Split(p, "/") {
		cur = filepath.Join(cur, v)
		s, e := f.root.Lstat(cur)
		if e != nil {
			return e
		}
		if s.Mode()&fs.ModeSymlink != 0 {
			return fmt.Errorf("拒绝符号链接: %s", p)
		}
	}
	return nil
}
func (f *Folder) Open(p string) (io.ReadCloser, error) {
	if e := f.check(p); e != nil {
		return nil, e
	}
	return f.root.Open(p)
}
func (f *Folder) List(p string) ([]Entry, error) {
	if e := f.check(p); e != nil {
		return nil, e
	}
	v, e := f.root.Open(p)
	if e != nil {
		return nil, e
	}
	defer v.Close()
	ds, e := v.ReadDir(-1)
	var out []Entry
	for _, d := range ds {
		if d.Type()&fs.ModeSymlink != 0 {
			return nil, fmt.Errorf("拒绝符号链接 %s", d.Name())
		}
		s, err := d.Info()
		if err != nil {
			return nil, err
		}
		if !s.IsDir() && !s.Mode().IsRegular() {
			return nil, fmt.Errorf("拒绝特殊文件 %s", d.Name())
		}
		out = append(out, Entry{d.Name(), s.Size(), d.IsDir()})
	}
	return out, e
}
func (f *Folder) Close() error { return f.root.Close() }
func Open(p string) (Source, error) {
	s, e := os.Stat(p)
	if e != nil {
		return nil, e
	}
	if s.IsDir() {
		return NewFolder(p)
	}
	f, e := os.Open(p)
	if e != nil {
		return nil, e
	}
	return FromFile(f)
}
func FromFile(f *os.File) (Source, error) {
	return FromReader(f, f.Close)
}
func FromReader(r io.ReaderAt, closeFn func() error) (Source, error) {
	x, e := NewExt4(r)
	if e != nil {
		closeFn()
		return nil, e
	}
	x.close = closeFn
	if n, e := x.node("var/lib/rp-ylx-data-volume/data-volume.img"); e == nil {
		inner, e := NewExt4(n)
		if e != nil {
			closeFn()
			return nil, e
		}
		inner.outer = x
		inner.OuterJournalPending = x.JournalPending
		inner.close = closeFn
		return inner, nil
	}
	return x, nil
}

// MetadataSources exposes filesystem roots for read-only catalog lookup. The
// caller must not close these views; ownership remains with the original Source.
func MetadataSources(s Source) []Source {
	if x, ok := s.(*Ext4); ok && x.outer != nil {
		return []Source{x.outer, x}
	}
	return []Source{s}
}

func Warnings(s Source) string {
	if x, ok := s.(*Ext4); ok && (x.OuterJournalPending || x.JournalPending) {
		return "存储设备有尚未恢复的写入日志，最近录制可能暂不可见。为保护源数据，本程序不会自动修复。"
	}
	return ""
}

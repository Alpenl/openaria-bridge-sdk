package diagnostics

import (
	"fmt"
	"os"
	"path/filepath"
	"sync"
)

const Limit = 2 * 1024 * 1024

type Writer struct {
	mu     sync.Mutex
	path   string
	failed error
}

func New(path string) (*Writer, error) {
	if e := os.MkdirAll(filepath.Dir(path), 0700); e != nil {
		return nil, e
	}
	f, e := os.OpenFile(path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0600)
	if e != nil {
		return nil, e
	}
	e = f.Close()
	return &Writer{path: path}, e
}
func (w *Writer) Write(b []byte) (int, error) {
	w.mu.Lock()
	defer w.mu.Unlock()
	n, e := w.write(b)
	w.failed = e
	return n, e
}
func (w *Writer) write(b []byte) (int, error) {
	if len(b) > Limit {
		b = b[:Limit]
	}
	if s, e := os.Stat(w.path); e == nil && s.Size()+int64(len(b)) > Limit {
		if e = os.Remove(w.path + ".1"); e != nil && !os.IsNotExist(e) {
			return 0, e
		}
		if e = os.Rename(w.path, w.path+".1"); e != nil {
			return 0, e
		}
	}
	f, e := os.OpenFile(w.path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0600)
	if e != nil {
		return 0, e
	}
	n, e := f.Write(b)
	ce := f.Close()
	if e == nil {
		e = ce
	}
	return n, e
}
func (w *Writer) Location() string {
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.failed != nil {
		return fmt.Sprintf("日志写入失败：%v", w.failed)
	}
	return "日志：" + w.path
}

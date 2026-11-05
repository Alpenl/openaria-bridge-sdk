package core

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"openaria-exporter/internal/source"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

type observedSource struct {
	source.Source
	active atomic.Int32
	open   func(string)
}

func (s *observedSource) List(p string) ([]source.Entry, error) {
	if !s.active.CompareAndSwap(0, 1) {
		panic("concurrent source access")
	}
	defer s.active.Store(0)
	return s.Source.List(p)
}
func (s *observedSource) Open(p string) (io.ReadCloser, error) {
	if !s.active.CompareAndSwap(0, 1) {
		panic("concurrent source access")
	}
	if s.open != nil {
		s.open(p)
	}
	r, e := s.Source.Open(p)
	if e != nil {
		s.active.Store(0)
		return nil, e
	}
	return &observedReader{ReadCloser: r, s: s}, nil
}

type observedReader struct {
	io.ReadCloser
	s *observedSource
}

func (r *observedReader) Close() error { e := r.ReadCloser.Close(); r.s.active.Store(0); return e }
func await(t *testing.T, c <-chan struct{}) {
	t.Helper()
	select {
	case <-c:
	case <-time.After(10 * time.Second):
		t.Fatal("barrier timeout")
	}
}

func TestTransferOverlapAndReceipt(t *testing.T) {
	for _, serial := range []bool{false, true} {
		t.Run(map[bool]string{false: "pipeline", true: "serial"}[serial], func(t *testing.T) {
			src, v := fixture(t, false)
			entered, release, bOpened := make(chan struct{}), make(chan struct{}), make(chan struct{})
			waiting := make(chan struct{}, 1)
			var thirdOpened atomic.Bool
			var verified atomic.Bool
			s := &observedSource{Source: src, open: func(p string) {
				if p == "manifest.json" {
					thirdOpened.Store(true)
				}
				if p == "left.mp4" {
					if serial && !verified.Load() {
						panic("serial overlap")
					}
					close(bOpened)
				}
			}}
			hooks := &transferTestHooks{wait: func(Artifact) {
				select {
				case waiting <- struct{}{}:
				default:
				}
			}, verify: func(a Artifact) error {
				if a.Path == "audio.flac" {
					close(entered)
					<-release
					verified.Store(true)
				}
				return nil
			}}
			out := t.TempDir()
			done := make(chan error, 1)
			go func() {
				_, e := Export(t.Context(), s, v, Options{Output: out, Serial: serial, transferTest: hooks})
				done <- e
			}()
			await(t, entered)
			if !serial {
				await(t, bOpened)
				await(t, waiting)
				if thirdOpened.Load() {
					t.Fatal("third file opened while both slots occupied")
				}
			}
			close(release)
			if e := <-done; e != nil {
				t.Fatal(e)
			}
			b, e := os.ReadFile(filepath.Join(out, v.ID+"-archive", "receipt.json"))
			if e != nil {
				t.Fatal(e)
			}
			var r Receipt
			if e = json.Unmarshal(b, &r); e != nil {
				t.Fatal(e)
			}
			if len(r.Files) != 4 || len(r.Provenance) != 4 {
				t.Fatalf("incomplete receipt: %+v", r)
			}
		})
	}
}

func TestTransferBudgetAndOversize(t *testing.T) {
	for _, budget := range []int64{40, 1} {
		t.Run(fmt.Sprint(budget), func(t *testing.T) {
			src, v := fixture(t, false)
			entered, release, waiting := make(chan struct{}), make(chan struct{}), make(chan struct{}, 1)
			var opened atomic.Bool
			s := &observedSource{Source: src, open: func(p string) {
				if p == "left.mp4" {
					opened.Store(true)
				}
			}}
			hooks := &transferTestHooks{budget: budget, verify: func(a Artifact) error {
				if a.Path == "audio.flac" {
					close(entered)
					<-release
				}
				return nil
			}, wait: func(a Artifact) {
				select {
				case waiting <- struct{}{}:
				default:
				}
			}}
			done := make(chan error, 1)
			go func() {
				_, e := Export(t.Context(), s, v, Options{Output: t.TempDir(), transferTest: hooks})
				done <- e
			}()
			await(t, entered)
			await(t, waiting) // coordinator is waiting on A before opening B
			if opened.Load() {
				t.Fatal("budget/oversize allowed next source")
			}
			close(release)
			if e := <-done; e != nil {
				t.Fatal(e)
			}
		})
	}
}

func TestTransferCancelJoinsBeforeUnlockAndPreservesFailure(t *testing.T) {
	src, v := fixture(t, false)
	out := t.TempDir()
	entered, release, copyB := make(chan struct{}), make(chan struct{}), make(chan struct{})
	ctx, cancel := context.WithCancel(t.Context())
	defer cancel()
	fault := errors.New("target read failure")
	var callbacks atomic.Int32
	s := &observedSource{Source: src, open: func(p string) {
		if p == "left.mp4" {
			<-entered
			cancel()
			close(copyB)
		}
	}}
	done := make(chan error, 1)
	go func() {
		_, e := Export(ctx, s, v, Options{Output: out, Progress: func(Progress) { callbacks.Add(1) }, transferTest: &transferTestHooks{verify: func(a Artifact) error {
			if a.Path == "audio.flac" {
				close(entered)
				<-release
				return fault
			}
			return nil
		}}})
		done <- e
	}()
	await(t, copyB)
	lock, e := os.OpenFile(filepath.Join(out, "."+v.ID+"-archive.partial", ".lock"), os.O_RDWR, 0)
	if e != nil {
		t.Fatal(e)
	}
	if e = lockFile(lock); e == nil {
		lock.Close()
		t.Fatal("lock released before join")
	}
	lock.Close()
	select {
	case e := <-done:
		t.Fatalf("returned before join: %v", e)
	default:
	}
	close(release)
	if e = <-done; !errors.Is(e, fault) {
		t.Fatalf("lost target error: %v", e)
	}
	n := callbacks.Load()
	if _, e = os.Stat(filepath.Join(out, v.ID+"-archive")); !os.IsNotExist(e) {
		t.Fatal("failure published")
	}
	// Same staging can be locked and fully recovered immediately after return.
	p, e := Export(t.Context(), src, v, Options{Output: out})
	if e != nil {
		t.Fatal(e)
	}
	if callbacks.Load() != n {
		t.Fatal("tail callback")
	}
	b, e := os.ReadFile(filepath.Join(p, "receipt.json"))
	if e != nil || !strings.Contains(string(b), "manifest.json") {
		t.Fatalf("receipt: %s %v", b, e)
	}
}

func TestTransferTargetCorruptionNotPublished(t *testing.T) {
	src, v := fixture(t, false)
	out := t.TempDir()
	_, e := Export(t.Context(), src, v, Options{Output: out, transferTest: &transferTestHooks{verify: func(a Artifact) error {
		if a.Path == "audio.flac" {
			return os.WriteFile(filepath.Join(out, "."+v.ID+"-archive.partial", "work", "transfer-0"), []byte("damaged"), 0600)
		}
		return nil
	}}})
	if e == nil || !strings.Contains(e.Error(), "目标复核失败") {
		t.Fatalf("target corruption: %v", e)
	}
	if _, e = os.Stat(filepath.Join(out, v.ID+"-archive")); !os.IsNotExist(e) {
		t.Fatal("corruption published")
	}
	if _, e = os.Stat(filepath.Join(out, "."+v.ID+"-archive.partial", "source", "audio.flac")); !os.IsNotExist(e) {
		t.Fatal("corrupt file renamed")
	}
	if _, e = Export(t.Context(), src, v, Options{Output: out}); e != nil {
		t.Fatal(e)
	}
}

func TestTransferMixedRecoveryReceipt(t *testing.T) {
	src, v := fixture(t, false)
	out := t.TempDir()
	p, e := Export(t.Context(), src, v, Options{Output: out})
	if e != nil {
		t.Fatal(e)
	}
	before, e := os.ReadFile(filepath.Join(p, "receipt.json"))
	if e != nil {
		t.Fatal(e)
	}
	stage := filepath.Join(out, "."+v.ID+"-archive.partial")
	if e = os.Rename(p, stage); e != nil {
		t.Fatal(e)
	}
	for _, name := range []string{"receipt.json", "source/left.mp4"} {
		if e = os.Remove(filepath.Join(stage, name)); e != nil {
			t.Fatal(e)
		}
	}
	entered, release, waiting := make(chan struct{}), make(chan struct{}), make(chan struct{}, 1)
	var recovered atomic.Bool
	done := make(chan error, 1)
	go func() {
		_, e := Export(t.Context(), src, v, Options{Output: out, Progress: func(p Progress) {
			if p.File == "manifest.json" && p.Stage == "复核恢复文件" {
				recovered.Store(true)
			}
		}, transferTest: &transferTestHooks{
			verify: func(a Artifact) error {
				if a.Path == "left.mp4" {
					close(entered)
					<-release
				}
				return nil
			},
			wait: func(Artifact) {
				select {
				case waiting <- struct{}{}:
				default:
				}
			},
		}})
		done <- e
	}()
	await(t, entered)
	await(t, waiting)
	if recovered.Load() {
		t.Fatal("recovery entered before drain")
	}
	close(release)
	if e = <-done; e != nil {
		t.Fatal(e)
	}
	after, e := os.ReadFile(filepath.Join(p, "receipt.json"))
	if e != nil {
		t.Fatal(e)
	}
	if string(before) != string(after) {
		t.Fatal("recovery changed receipt")
	}
}

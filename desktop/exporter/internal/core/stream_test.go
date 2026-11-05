package core

import (
	"bytes"
	"context"
	"io"
	"net/http"
	"openaria-exporter/internal/source"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"
)

func streamFixture(t *testing.T) (source.Source, Session, []byte) {
	t.Helper()
	root := t.TempDir()
	data := bytes.Repeat([]byte("range-test-data-"), (streamBlock*3)/16+1)
	if e := os.WriteFile(filepath.Join(root, "media.bin"), data, 0600); e != nil {
		t.Fatal(e)
	}
	s, e := source.NewFolder(root)
	if e != nil {
		t.Fatal(e)
	}
	t.Cleanup(func() { s.Close() })
	var v Session
	v.Dir = "."
	a := Artifact{Path: "media.bin", Bytes: int64(len(data))}
	v.Artifacts = []Artifact{a}
	seg := Segment{Index: 0, Start: 0, End: 1}
	seg.Artifacts.Left = a
	seg.Artifacts.Right = a
	v.Manifest.Video.Segments = []Segment{seg}
	return s, v, data
}
func TestStreamRangesAndIsolation(t *testing.T) {
	s, v, data := streamFixture(t)
	g, e := newMediaStream(t.Context(), s, []Session{v})
	if e != nil {
		t.Fatal(e)
	}
	defer g.Close()
	client := &http.Client{Timeout: 5 * time.Second}
	for _, tc := range []struct {
		r      string
		lo, hi int
		status int
	}{{"bytes=17-1023", 17, 1024, 206}, {"bytes=-27", len(data) - 27, len(data), 206}, {"bytes=999999999-", 0, 0, 416}, {"bytes=0-1,4-5", 0, 0, 416}} {
		req, _ := http.NewRequest("GET", g.URL(v.Dir, "media.bin"), nil)
		req.Header.Set("Range", tc.r)
		r, e := client.Do(req)
		if e != nil {
			t.Fatal(e)
		}
		b, e := io.ReadAll(r.Body)
		r.Body.Close()
		if e != nil || r.StatusCode != tc.status {
			t.Fatalf("%s: %d %v", tc.r, r.StatusCode, e)
		}
		if tc.status == 206 && !bytes.Equal(b, data[tc.lo:tc.hi]) {
			t.Fatal("wrong range")
		}
	}
	r, e := client.Get(g.base + "../../etc/passwd")
	if e != nil {
		t.Fatal(e)
	}
	r.Body.Close()
	if r.StatusCode != 404 {
		t.Fatal("unlisted resource served")
	}
	var wg sync.WaitGroup
	for range 8 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			r, e := client.Get(g.URL(v.Dir, "media.bin"))
			if e != nil {
				t.Error(e)
				return
			}
			b, e := io.ReadAll(r.Body)
			r.Body.Close()
			if e != nil || !bytes.Equal(b, data) {
				t.Error("concurrent range stream mismatch", e)
			}
		}()
	}
	wg.Wait()
	if g.Err() != nil {
		t.Fatal(g.Err())
	}
}

type gatedSource struct {
	source.Source
	entered, release chan struct{}
	once             sync.Once
}

func (s *gatedSource) Open(p string) (io.ReadCloser, error) {
	r, e := s.Source.Open(p)
	if e != nil {
		return nil, e
	}
	return &gatedReader{ReadCloser: r, reader: r.(io.ReaderAt), owner: s}, nil
}

type gatedReader struct {
	io.ReadCloser
	reader io.ReaderAt
	owner  *gatedSource
}

func (r *gatedReader) ReadAt(p []byte, off int64) (int, error) {
	r.owner.once.Do(func() { close(r.owner.entered) })
	<-r.owner.release
	return r.reader.ReadAt(p, off)
}
func TestStreamCloseJoinsPhysicalRead(t *testing.T) {
	s, v, _ := streamFixture(t)
	gate := &gatedSource{Source: s, entered: make(chan struct{}), release: make(chan struct{})}
	g, e := newMediaStream(context.Background(), gate, []Session{v})
	if e != nil {
		t.Fatal(e)
	}
	readDone := make(chan struct{})
	go func() { defer close(readDone); _, _ = g.block(streamKey{0, 0}) }()
	<-gate.entered
	closed := make(chan struct{})
	go func() { g.Close(); close(closed) }()
	select {
	case <-closed:
		t.Fatal("closed before read joined")
	case <-time.After(30 * time.Millisecond):
	}
	close(gate.release)
	<-closed
	<-readDone
	if _, e = g.block(streamKey{0, 0}); e == nil {
		t.Fatal("closed stream read")
	}
}
func TestVideoDoesNotHashMedia(t *testing.T) {
	s, v := fixture(t, true)
	for i := range v.Artifacts {
		v.Artifacts[i].SHA = strings.Repeat("0", 64)
	}
	if _, e := Export(t.Context(), s, v, Options{Output: t.TempDir(), VideoOnly: true}); e != nil {
		t.Fatal("media hash still required", e)
	}
}
func TestCaptureClockDoesNotPadOutput(t *testing.T) {
	s, v := fixture(t, true)
	v.Manifest.Video.Segments[0].End = 1.5
	p, e := Export(t.Context(), s, v, Options{Output: t.TempDir(), VideoOnly: true})
	if e != nil {
		t.Fatal(e)
	}
	info, e := quickProbe(t.Context(), filepath.Join(p, "video.mp4"))
	if e != nil {
		t.Fatal(e)
	}
	d, e := strconv.ParseFloat(info.Format.Duration, 64)
	if e != nil || d < 0.95 || d > 1.1 {
		t.Fatalf("capture clock padded output: %s", info.Format.Duration)
	}
	full, e := probe(t.Context(), filepath.Join(p, "video.mp4"))
	if e != nil {
		t.Fatal(e)
	}
	for _, stream := range full.Streams {
		if stream.CodecType == "video" && stream.Frames != "24" {
			t.Fatalf("lost frames: %s", stream.Frames)
		}
	}
}

func TestVideoBatchSharedSource(t *testing.T) {
	s, v := fixture(t, true)
	other := v
	other.ID = "other-session"
	completed := 0
	paths, e := ExportBatch(t.Context(), s, []Session{v, other}, Options{Output: t.TempDir(), batchTestWorkers: 2}, nil, func(_ int, _ string) { completed++ })
	if e != nil || len(paths) != 2 || completed != 2 {
		t.Fatalf("%v %v %d", paths, e, completed)
	}
	for _, p := range paths {
		if _, e = os.Stat(filepath.Join(p, "video.mp4")); e != nil {
			t.Fatal(e)
		}
	}
}

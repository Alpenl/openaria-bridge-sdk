package core

import (
	"container/list"
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"openaria-exporter/internal/source"
	"path"
	"strconv"
	"strings"
	"sync"
	"time"
)

const streamBlock = 4 << 20
const streamCacheBytes = 256 << 20

type streamKey struct {
	file   int
	offset int64
}
type streamBlockData struct {
	key  streamKey
	data []byte
}
type streamFile struct {
	path string
	size int64
}

// One cache and one read lock coordinate all eyes, audio and sessions. Only
// compressed bytes are cached; decoded frames belong to FFmpeg's bounded queues.
type mediaStream struct {
	ctx       context.Context
	cancel    context.CancelFunc
	source    source.Source
	mu        sync.Mutex
	files     []streamFile
	index     map[string]int
	cache     map[streamKey]*list.Element
	lru       *list.List
	bytes     int
	err       error
	readBytes int64
	prefetch  chan streamKey
	done      chan struct{}
	server    *http.Server
	base      string
}

func newMediaStream(ctx context.Context, s source.Source, vs []Session) (*mediaStream, error) {
	ctx, cancel := context.WithCancel(ctx)
	g := &mediaStream{ctx: ctx, cancel: cancel, source: s, index: map[string]int{}, cache: map[streamKey]*list.Element{}, lru: list.New(), prefetch: make(chan streamKey, 16), done: make(chan struct{})}
	for _, v := range vs {
		for _, a := range mediaArtifacts(v) {
			p := path.Join(v.Dir, a.Path)
			if source.Safe(a.Path) != nil || source.Safe(p) != nil || a.Bytes <= 0 {
				cancel()
				return nil, fmt.Errorf("无效媒体路径或大小 %s", a.Path)
			}
			if n, ok := g.index[p]; ok {
				if g.files[n].size != a.Bytes {
					cancel()
					return nil, fmt.Errorf("媒体大小冲突")
				}
				continue
			}
			g.index[p] = len(g.files)
			g.files = append(g.files, streamFile{p, a.Bytes})
		}
	}
	token := make([]byte, 24)
	if _, e := rand.Read(token); e != nil {
		cancel()
		return nil, e
	}
	l, e := net.Listen("tcp4", "127.0.0.1:0")
	if e != nil {
		cancel()
		return nil, e
	}
	prefix := "/" + hex.EncodeToString(token) + "/"
	g.base = "http://" + l.Addr().String() + prefix
	slots := make(chan struct{}, 8)
	g.server = &http.Server{ReadHeaderTimeout: 5 * time.Second, IdleTimeout: 10 * time.Second, Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if (r.Method != "GET" && r.Method != "HEAD") || !strings.HasPrefix(r.URL.Path, prefix) {
			http.NotFound(w, r)
			return
		}
		id, e := strconv.Atoi(strings.TrimPrefix(r.URL.Path, prefix))
		if e != nil || id < 0 || id >= len(g.files) {
			http.NotFound(w, r)
			return
		}
		if strings.Contains(r.Header.Get("Range"), ",") {
			http.Error(w, "single range required", http.StatusRequestedRangeNotSatisfiable)
			return
		}
		select {
		case slots <- struct{}{}:
			defer func() { <-slots }()
		case <-g.ctx.Done():
			http.Error(w, "closed", 503)
			return
		case <-r.Context().Done():
			return
		}
		f := g.files[id]
		w.Header().Set("Content-Type", "application/octet-stream")
		http.ServeContent(w, r, "media", time.Time{}, io.NewSectionReader(streamReader{g, id}, 0, f.size))
	})}
	go func() { _ = g.server.Serve(l) }()
	go func() {
		defer close(g.done)
		for {
			select {
			case <-ctx.Done():
				return
			case key := <-g.prefetch:
				_, _ = g.block(key)
			}
		}
	}()
	return g, nil
}
func (g *mediaStream) URL(dir, p string) string {
	return g.base + strconv.Itoa(g.index[path.Join(dir, p)])
}
func (g *mediaStream) Close() {
	g.cancel()
	_ = g.server.Close()
	<-g.done
	// Join any physical read before the caller can close the source.
	g.mu.Lock()
	g.cache = nil
	g.lru.Init()
	g.mu.Unlock()
}
func (g *mediaStream) Err() error       { g.mu.Lock(); defer g.mu.Unlock(); return g.err }
func (g *mediaStream) ReadBytes() int64 { g.mu.Lock(); defer g.mu.Unlock(); return g.readBytes }
func (g *mediaStream) block(key streamKey) ([]byte, error) {
	g.mu.Lock()
	defer g.mu.Unlock()
	if e := g.ctx.Err(); e != nil {
		return nil, e
	}
	if v := g.cache[key]; v != nil {
		g.lru.MoveToFront(v)
		return v.Value.(streamBlockData).data, nil
	}
	f := g.files[key.file]
	n := min(int64(streamBlock), f.size-key.offset)
	if n <= 0 {
		return nil, io.EOF
	}
	r, e := g.source.Open(f.path)
	if e != nil {
		g.err = e
		return nil, e
	}
	reader, ok := r.(io.ReaderAt)
	if !ok {
		r.Close()
		g.err = fmt.Errorf("源不支持随机只读访问")
		return nil, g.err
	}
	data := make([]byte, int(n))
	got, e := reader.ReadAt(data, key.offset)
	ce := r.Close()
	g.readBytes += int64(got)
	if got != len(data) && e == nil {
		e = io.ErrUnexpectedEOF
	}
	if e != nil || ce != nil {
		g.err = errors.Join(e, ce)
		return nil, g.err
	}
	for g.bytes+len(data) > streamCacheBytes && g.lru.Len() > 0 {
		old := g.lru.Back()
		b := old.Value.(streamBlockData)
		g.bytes -= len(b.data)
		delete(g.cache, b.key)
		g.lru.Remove(old)
	}
	g.cache[key] = g.lru.PushFront(streamBlockData{key, data})
	g.bytes += len(data)
	return data, nil
}

type streamReader struct {
	g  *mediaStream
	id int
}

func (r streamReader) ReadAt(p []byte, off int64) (int, error) {
	if off < 0 {
		return 0, fmt.Errorf("negative offset")
	}
	size := r.g.files[r.id].size
	done := 0
	for done < len(p) {
		if off >= size {
			return done, io.EOF
		}
		start := off / int64(streamBlock) * int64(streamBlock)
		b, e := r.g.block(streamKey{r.id, start})
		if e != nil {
			return done, e
		}
		n := copy(p[done:], b[off-start:])
		done += n
		off += int64(n)
		if next := start + streamBlock; next < size && off-int64(n) == start {
			select {
			case r.g.prefetch <- streamKey{r.id, next}:
			default:
			}
		}
	}
	return done, nil
}

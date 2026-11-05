package core

import (
	"context"
	"fmt"
	"openaria-exporter/internal/source"
	"path"
	"sync"
)

// ExportBatch shares one bounded compressed-data cache and one physical source
// reader. Callbacks are serialized even when encoders run concurrently.
func ExportBatch(ctx context.Context, s source.Source, vs []Session, o Options, progress func(int, Progress), complete func(int, string)) ([]string, error) {
	if len(vs) == 0 {
		return nil, fmt.Errorf("没有选中的录像")
	}
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	g, e := newMediaStream(ctx, s, vs)
	if e != nil {
		return nil, e
	}
	defer g.Close()
	o.VideoOnly = true
	o.stream = g
	workers := 1
	var duration float64
	for _, v := range vs {
		duration += mediaDuration(v)
	}
	if len(vs) > 1 && duration >= 120 && !o.Serial {
		ff, e := mediaTool("ffmpeg")
		if e != nil {
			return nil, e
		}
		p, e := chooseEncoder(ctx, ff)
		if e != nil {
			return nil, e
		}
		workers = chooseBatchWorkers(ctx, ff, p, g, vs[0])
	}
	if o.batchTestWorkers > 0 {
		workers = o.batchTestWorkers
	}
	workers = min(workers, len(vs), 2)
	var wg sync.WaitGroup
	var mu sync.Mutex
	results := make([]string, len(vs))
	next := 0
	var first error
	for range workers {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for {
				mu.Lock()
				if next >= len(vs) || ctx.Err() != nil {
					mu.Unlock()
					return
				}
				i := next
				next++
				mu.Unlock()
				opts := o
				opts.Progress = func(p Progress) {
					mu.Lock()
					defer mu.Unlock()
					if progress != nil {
						progress(i, p)
					}
				}
				if i+1 < len(vs) {
					g.prefetchSession(vs[i+1])
				}
				dst, e := Export(ctx, s, vs[i], opts)
				mu.Lock()
				if e != nil {
					if first == nil {
						first = e
					}
					cancel()
				} else {
					results[i] = dst
					if complete != nil {
						complete(i, dst)
					}
				}
				mu.Unlock()
			}
		}()
	}
	wg.Wait()
	if first != nil {
		return results, first
	}
	if e = ctx.Err(); e != nil {
		return results, e
	}
	return results, nil
}
func (g *mediaStream) prefetchSession(v Session) {
	// Only the next recording's first compressed blocks; demand reads keep a
	// bounded head start instead of copying a whole recording before encoding.
	if len(v.Manifest.Video.Segments) == 0 {
		return
	}
	seg := v.Manifest.Video.Segments[0]
	for _, p := range []string{seg.Artifacts.Left.Path, seg.Artifacts.Right.Path} {
		id, ok := g.index[path.Join(v.Dir, p)]
		if ok {
			select {
			case g.prefetch <- streamKey{id, 0}:
			default:
			}
		}
	}
}

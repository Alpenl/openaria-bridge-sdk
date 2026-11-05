package core

import (
	"context"
	"fmt"
	"log"
	"math"
	"openaria-exporter/internal/process"
	"runtime"
	"sync"
	"time"
)

var batchSelection struct {
	sync.Mutex
	key     string
	workers int
}

// Measure the actual decode + stereo composition + encode path. A solid-color
// encoder-only trial overestimates concurrency gains on shared media engines.
func chooseBatchWorkers(ctx context.Context, ff string, p encoderProfile, g *mediaStream, v Session) int {
	if p.name == "libx264" || runtime.NumCPU() < 4 || len(v.Manifest.Video.Segments) == 0 {
		return 1
	}
	seg := v.Manifest.Video.Segments[0]
	info, e := quickProbe(ctx, g.URL(v.Dir, seg.Artifacts.Left.Path))
	if e != nil {
		return 1
	}
	w, h, codec := videoShape(info)
	key := fmt.Sprintf("%s/%s/%v/%dx%d/%s", ff, p.name, p.global, w, h, codec)
	batchSelection.Lock()
	defer batchSelection.Unlock()
	if batchSelection.key == key {
		return batchSelection.workers
	}
	seconds := math.Min(3, seg.End-seg.Start)
	if seconds < 1 {
		return 1
	}
	args := append([]string{"-nostdin", "-v", "error", "-filter_complex_threads", "2"}, p.global...)
	for _, a := range []Artifact{seg.Artifacts.Left, seg.Artifacts.Right} {
		if p.useHardwareDecode(codec, w, h) {
			args = append(args, p.decoderArgs(false)...)
		}
		args = append(args, "-threads", "2", "-i", g.URL(v.Dir, a.Path))
	}
	filter := "[0:v]setpts=PTS-STARTPTS[l];[1:v]setpts=PTS-STARTPTS[r];[l][r]hstack=inputs=2[v]"
	mapped := "[v]"
	if p.upload {
		filter += ";[v]format=nv12,hwupload[hw]"
		mapped = "[hw]"
	}
	args = append(args, "-filter_complex", filter, "-map", mapped, "-t", fmt.Sprint(seconds), "-an")
	args = append(args, p.args(mediaBitrate(v))...)
	args = append(args, "-f", "null", "-")
	trial := func(n int) (time.Duration, bool) {
		c, cancel := context.WithTimeout(ctx, 10*time.Second)
		defer cancel()
		start := time.Now()
		var wg sync.WaitGroup
		ok := make(chan bool, n)
		for range n {
			wg.Add(1)
			go func() {
				defer wg.Done()
				cmd := process.CommandContext(c, ff, args...)
				cmd.Env = mediaEnvironment()
				e := cmd.Run()
				ok <- e == nil
			}()
		}
		wg.Wait()
		close(ok)
		success := true
		for result := range ok {
			success = success && result
		}
		return time.Since(start), success
	}
	// Warm the decoder, encoder and compressed read cache before comparing.
	if _, ok := trial(1); !ok {
		return 1
	}
	single, ok := trial(1)
	if !ok {
		return 1
	}
	parallel, ok := trial(2)
	workers := 1
	if ok && parallel < single*16/10 {
		workers = 2
	}
	log.Printf("encoder concurrency trial name=%s resolution=%dx%d single=%s two=%s workers=%d", p.name, w*2, h, single, parallel, workers)
	if ctx.Err() == nil {
		batchSelection.key, batchSelection.workers = key, workers
	}
	return workers
}

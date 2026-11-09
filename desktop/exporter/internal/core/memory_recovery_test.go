package core

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func pinSoftwareEncoder(t *testing.T) {
	t.Helper()
	ff, err := mediaTool("ffmpeg")
	if err != nil {
		t.Fatal(err)
	}
	encoderSelection.Lock()
	oldPath, oldProfile, oldReady := encoderSelection.path, encoderSelection.profile, encoderSelection.ready
	encoderSelection.path, encoderSelection.profile, encoderSelection.ready = ff, softwareEncoder(), true
	encoderSelection.Unlock()
	t.Cleanup(func() {
		encoderSelection.Lock()
		encoderSelection.path, encoderSelection.profile, encoderSelection.ready = oldPath, oldProfile, oldReady
		encoderSelection.Unlock()
	})
}

func TestEncodingAllocationRecovery(t *testing.T) {
	requireMediaTools(t)
	pinSoftwareEncoder(t)
	s, v := fixture(t, true)
	oom := errors.New("FFmpeg: x264 [error]: malloc of size 12163264 failed")
	dts := errors.New("Non-monotonic DTS; previous: 74752, current: 74752")
	for _, tc := range []struct {
		name           string
		failures       []error
		repaired, fail bool
	}{
		{"ordinary", []error{oom}, false, false},
		{"during-timeline-repair", []error{dts, oom}, true, false},
		{"low-memory-needs-timeline-repair", []error{oom, dts}, true, false},
		{"still-out-of-memory", []error{oom, oom}, false, true},
		{"unrelated-error", []error{errors.New("Invalid data found when processing input")}, false, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			g, err := newMediaStream(t.Context(), s, []Session{v})
			if err != nil {
				t.Fatal(err)
			}
			defer g.Close()
			dir := t.TempDir()
			if err := os.Mkdir(filepath.Join(dir, "work"), 0700); err != nil {
				t.Fatal(err)
			}
			calls := 0
			sawLow := false
			run := func(ctx context.Context, ff string, args []string, progress func(Progress)) error {
				joined := strings.Join(args, " ")
				low := strings.Contains(joined, "-threads:v 1")
				if low {
					sawLow = true
					for _, want := range []string{"-c:v libx264", "-rc-lookahead 0", "-bf 0", "sync-lookahead=0", "-filter_complex_threads 1", "-threads 1", "-fps_mode passthrough", "-enc_time_base:v filter"} {
						if !strings.Contains(joined, want) {
							t.Fatalf("missing %s in %s", want, joined)
						}
					}
					if strings.Contains(joined, "-hwaccel") {
						t.Fatal("hardware in low-memory retry")
					}
				}
				calls++
				if calls <= len(tc.failures) {
					return tc.failures[calls-1]
				}
				return runMedia(ctx, ff, args, progress)
			}
			timing := &sessionTimings{}
			err = renderStreamingWithRunner(t.Context(), dir, v, g, nil, timing, run)
			expectedCalls := len(tc.failures)
			if !tc.fail {
				expectedCalls++
			}
			if (err != nil) != tc.fail || calls != expectedCalls {
				t.Fatalf("calls=%d want=%d err=%v", calls, expectedCalls, err)
			}
			if tc.fail {
				if _, err := os.Stat(filepath.Join(dir, "playback.mp4")); !os.IsNotExist(err) {
					t.Fatal("failed product published")
				}
				return
			}
			if !sawLow || !g.memoryConstrained() || timing.timelineRepaired != tc.repaired {
				t.Fatalf("low=%v repair=%v", sawLow, timing.timelineRepaired)
			}
			output := filepath.Join(dir, "playback.mp4")
			info, err := quickProbe(t.Context(), output)
			if err != nil {
				t.Fatal(err)
			}
			audio := false
			for _, stream := range info.Streams {
				audio = audio || stream.Type == "audio"
			}
			w, h, _ := videoShape(info)
			if !audio || w != 192 || h != 64 {
				t.Fatalf("incomplete output %+v", info)
			}
			if tc.repaired {
				if err := verifyRepairedPackets(t.Context(), output); err != nil {
					t.Fatal(err)
				}
			}
			// Subsequent sessions in this same batch must not try hardware or normal
			// software allocation again. Use the actual encoder and output validation.
			next := t.TempDir()
			if err := os.Mkdir(filepath.Join(next, "work"), 0700); err != nil {
				t.Fatal(err)
			}
			calls = len(tc.failures) // All fault injections consumed.
			sawLow = false
			if err := renderStreamingWithRunner(t.Context(), next, v, g, nil, &sessionTimings{}, run); err != nil {
				t.Fatal(err)
			}
			if !sawLow || calls != len(tc.failures)+1 {
				t.Fatal("memory mode not retained across sessions")
			}
		})
	}
}

func TestAllocationFailureClassification(t *testing.T) {
	for _, text := range []string{"x264 [error]: malloc of size 12163264 failed", "Error allocating the output packet", "Cannot allocate memory"} {
		if !allocationFailure(errors.New(text)) {
			t.Fatal(text)
		}
	}
	for _, text := range []string{"Cannot load cuMemAllocAsync", "Could not create the texture (80070057)", "Non-monotonic DTS", "Invalid data", "disk full"} {
		if allocationFailure(errors.New(text)) {
			t.Fatal(text)
		}
	}
	if allocationFailure(nil) {
		t.Fatal("nil")
	}
}

func TestStreamMemoryPressureBudget(t *testing.T) {
	s, v, _ := streamFixture(t)
	g, err := newMediaStream(t.Context(), s, []Session{v})
	if err != nil {
		t.Fatal(err)
	}
	defer g.Close()
	if _, err := g.block(streamKey{0, 0}); err != nil {
		t.Fatal(err)
	}
	g.releaseSession(v)
	if g.bytes != 0 || len(g.cache) != 0 {
		t.Fatal("completed recording cache retained")
	}
	if _, err := g.block(streamKey{0, 0}); err != nil {
		t.Fatal(err)
	}
	g.reduceMemory()
	if g.bytes != 0 || len(g.cache) != 0 {
		t.Fatal("memory recovery retained cache")
	}
	// Several distinct file IDs exercise eviction without a large media fixture.
	for i := 0; i < 12; i++ {
		g.files = append(g.files, g.files[0])
		if _, err := g.block(streamKey{len(g.files) - 1, 0}); err != nil {
			t.Fatal(err)
		}
		if g.bytes > 32<<20 {
			t.Fatal("low-memory cache grew", g.bytes)
		}
	}
	if g.Err() != nil {
		t.Fatal(fmt.Sprint(g.Err()))
	}
}

package core

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"openaria-exporter/internal/source"
	"os"
	"os/exec"
	"path/filepath"
	"testing"
)

func TestOrderedRejectsNonFiniteTimes(t *testing.T) {
	for _, v := range []Segment{{Index: 0, Start: 0, End: math.NaN()}, {Index: 0, Start: math.Inf(-1), End: 1}, {Index: -1, Start: 0, End: 1}} {
		if _, err := ordered([]Segment{v}); err == nil {
			t.Fatalf("accepted %+v", v)
		}
	}
}

func TestTimelineAuditBounds(t *testing.T) {
	for _, tc := range []struct {
		name  string
		times []float64
		fail  bool
	}{
		{"normal", []float64{0, .04, .08}, false},
		{"duplicate", []float64{0, .04, .04, .08}, false},
		{"small-regression", []float64{0, .04, .0395, .08}, false},
		{"clock-reset", []float64{0, 1, 0}, true},
		{"large-gap", []float64{0, 60}, true},
		{"missing", []float64{math.NaN()}, true},
		{"infinite", []float64{math.Inf(1)}, true},
		{"negative-start", []float64{-2, -1.96, -1.92}, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var a timelineAudit
			var err error
			for _, x := range tc.times {
				err = a.add(x)
				if err != nil {
					break
				}
			}
			if (err != nil) != tc.fail {
				t.Fatal(err)
			}
		})
	}
	var a timelineAudit
	for i := 0; i < 1000; i++ {
		if err := a.add(0); err != nil {
			return
		}
	}
	t.Fatal("unbounded cumulative correction accepted")
}

func TestDuplicateDecodedTimestampsRecovery(t *testing.T) {
	requireMediaTools(t)
	// Pin this test to software so a tolerant hardware encoder cannot mask the bug.
	encoderSelection.Lock()
	oldPath, oldProfile, oldReady := encoderSelection.path, encoderSelection.profile, encoderSelection.ready
	ff, err := mediaTool("ffmpeg")
	if err != nil {
		t.Fatal(err)
	}
	encoderSelection.path, encoderSelection.profile, encoderSelection.ready = ff, softwareEncoder(), true
	encoderSelection.Unlock()
	defer func() {
		encoderSelection.Lock()
		encoderSelection.path, encoderSelection.profile, encoderSelection.ready = oldPath, oldProfile, oldReady
		encoderSelection.Unlock()
	}()
	dir := t.TempDir()
	file := filepath.Join(dir, "duplicate.mkv")
	cmd := exec.CommandContext(t.Context(), "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=96x64:r=24:d=3", "-vf", "setpts=if(eq(N\\,24)\\,23\\,N)/(24*TB)", "-fps_mode", "passthrough", "-enc_time_base:v", "filter", "-c:v", "libx264", "-bf", "0", file)
	if b, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("fixture %v %s", err, b)
	}
	old := exec.CommandContext(t.Context(), "ffmpeg", "-v", "error", "-xerror", "-i", file, "-i", file, "-filter_complex", "[0:v]setpts=PTS-STARTPTS[l];[1:v]setpts=PTS-STARTPTS[r];[l][r]hstack=inputs=2:shortest=1[v]", "-map", "[v]", "-fps_mode", "passthrough", "-enc_time_base:v", "filter", "-c:v", "libx264", filepath.Join(t.TempDir(), "old.mp4"))
	b, e := old.CombinedOutput()
	if e == nil || !timestampFailure(fmt.Errorf("%s", b)) {
		t.Fatalf("0.1.7 failure not reproduced %v %s", e, b)
	}
	audioFile := filepath.Join(dir, "audio.flac")
	if b, err := exec.CommandContext(t.Context(), "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=3", "-c:a", "flac", audioFile).CombinedOutput(); err != nil {
		t.Fatalf("audio fixture: %v %s", err, b)
	}
	audioInfo, err := os.Stat(audioFile)
	if err != nil {
		t.Fatal(err)
	}
	aa := Artifact{Path: "audio.flac", Bytes: audioInfo.Size()}
	info, err := os.Stat(file)
	if err != nil {
		t.Fatal(err)
	}
	a := Artifact{Path: "duplicate.mkv", Bytes: info.Size()}
	seg := Segment{Index: 0, Start: 0, End: 3}
	seg.Artifacts.Left = a
	seg.Artifacts.Right = a
	v := Session{ID: "duplicate", Dir: ".", Raw: []byte(`{"test":"duplicate"}`), Artifacts: []Artifact{a}}
	v.Manifest.Video.Segments = []Segment{seg}
	s, err := source.NewFolder(dir)
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
	for _, count := range []int{1, 2} {
		t.Run(fmt.Sprint(count), func(t *testing.T) {
			v.Manifest.Video.Segments = nil
			v.Manifest.Audio.Segments = nil
			for i := 0; i < count; i++ {
				x := seg
				x.Index = i
				x.Start = float64(i * 3)
				x.End = x.Start + 3
				v.Manifest.Video.Segments = append(v.Manifest.Video.Segments, x)
				if count == 2 {
					v.Manifest.Audio.Segments = append(v.Manifest.Audio.Segments, Segment{Index: i, Start: x.Start, End: x.End, Artifact: aa})
				}
			}
			dst, err := Export(t.Context(), s, v, Options{Output: t.TempDir(), VideoOnly: true, RawFallback: true})
			if err != nil {
				t.Fatal(err)
			}
			receiptBytes, err := os.ReadFile(filepath.Join(dst, "export.json"))
			if err != nil {
				t.Fatal(err)
			}
			var receipt struct {
				TimelineRepaired bool `json:"timeline_repaired"`
			}
			if err = json.Unmarshal(receiptBytes, &receipt); err != nil || !receipt.TimelineRepaired {
				t.Fatalf("missing repair provenance %s %v", receiptBytes, err)
			}
			b, err := exec.CommandContext(t.Context(), "ffprobe", "-v", "error", "-show_packets", "-select_streams", "v:0", "-show_entries", "packet=dts,pts", "-of", "json", filepath.Join(dst, "video.mp4")).Output()
			if err != nil {
				t.Fatal(err)
			}
			var result struct {
				Packets []struct{ DTS, PTS int64 } `json:"packets"`
			}
			if err = json.Unmarshal(b, &result); err != nil {
				t.Fatal(err)
			}
			if len(result.Packets) != 72*count {
				t.Fatalf("lost frames: %d", len(result.Packets))
			}
			for i, p := range result.Packets {
				if p.DTS != p.PTS {
					t.Fatal("reordering in recovery")
				}
				if i > 0 && p.DTS <= result.Packets[i-1].DTS {
					t.Fatal("DTS not increasing")
				}
			}
		})
	}
	// An artificial 500-second PTS excursion must not be flattened into an
	// apparently successful output. Preserve the original instead.
	badFile := filepath.Join(dir, "clock-reset.mp4")
	if b, err := exec.CommandContext(t.Context(), "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=96x64:r=24:d=3", "-c:v", "libx264", "-bf", "0", "-bsf:v", "setts=pts=if(between(N\\,24\\,26)\\,12288+(N-24)\\,PTS)", badFile).CombinedOutput(); err != nil {
		t.Fatalf("bad fixture %v %s", err, b)
	}
	badInfo, err := os.Stat(badFile)
	if err != nil {
		t.Fatal(err)
	}
	bad := Artifact{Path: "clock-reset.mp4", Bytes: badInfo.Size()}
	x := seg
	x.Artifacts.Left = bad
	x.Artifacts.Right = bad
	v.ID = "clock-reset"
	v.Artifacts = []Artifact{bad}
	v.Manifest.Video.Segments = []Segment{x}
	v.Manifest.Audio.Segments = nil
	dst, err := Export(t.Context(), s, v, Options{Output: t.TempDir(), VideoOnly: true, RawFallback: true})
	var raw *RawFallbackError
	if !errors.As(err, &raw) || dst == "" {
		t.Fatalf("unsafe timeline not preserved as raw: %s %v", dst, err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := auditTimeline(ctx, "missing"); err == nil {
		t.Fatal("canceled audit accepted")
	}
}

package core

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"openaria-exporter/internal/source"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
)

// requireMediaTools is shared by every test using generated media. Only missing
// executables skip: generation, codec and probe failures remain test failures.
func requireMediaTools(t *testing.T) {
	t.Helper()
	for _, name := range []string{"ffmpeg", "ffprobe"} {
		if _, err := exec.LookPath(name); err != nil {
			t.Skipf("媒体测试需要 PATH 中的 ffmpeg 和 ffprobe：缺少 %s (%v)", name, err)
		}
	}
}

func generateMedia(t *testing.T, dir string) {
	t.Helper()
	requireMediaTools(t)
	run := func(args ...string) {
		t.Helper()
		cmd := exec.CommandContext(t.Context(), "ffmpeg", append([]string{"-nostdin", "-v", "error", "-n"}, args...)...)
		if output, err := cmd.CombinedOutput(); err != nil {
			t.Fatalf("生成媒体 fixture 失败: %v\n%s", err, output)
		}
	}
	for _, eye := range []struct{ name, color string }{{"left.mp4", "red"}, {"right.mp4", "blue"}} {
		run("-f", "lavfi", "-i", "color=c="+eye.color+":s=96x64:r=24:d=1", "-frames:v", "24", "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", filepath.Join(dir, eye.name))
	}
	run("-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1", "-ac", "1", "-c:a", "flac", filepath.Join(dir, "audio.flac"))
}

func fixture(t *testing.T, media bool) (source.Source, Session) {
	t.Helper()
	d := t.TempDir()
	if media {
		generateMedia(t, d)
	}
	m := map[string]any{"schema": "ylx.device-session.v4", "session_id": "fixture-session", "sealed": true, "display_name": "测试录像"}
	artifact := func(name string) map[string]any {
		b := []byte("original bytes " + name)
		if media {
			var e error
			b, e = os.ReadFile(filepath.Join(d, name))
			if e != nil {
				t.Fatal(e)
			}
		}
		if !media {
			if err := os.WriteFile(filepath.Join(d, name), b, 0600); err != nil {
				t.Fatal(err)
			}
		}
		h := sha256.Sum256(b)
		return map[string]any{"path": name, "bytes": len(b), "sha256": hex.EncodeToString(h[:])}
	}
	m["video"] = map[string]any{"segments": []any{map[string]any{"index": 0, "start_time_seconds": 0, "end_time_seconds": 1, "artifacts": map[string]any{"left": artifact("left.mp4"), "right": artifact("right.mp4")}}}}
	m["audio"] = map[string]any{"segments": []any{map[string]any{"index": 0, "start_time_seconds": 0, "end_time_seconds": 1, "artifact": artifact("audio.flac")}}}
	b, _ := json.Marshal(m)
	os.WriteFile(filepath.Join(d, "manifest.json"), b, 0600)
	s, e := source.NewFolder(d)
	if e != nil {
		t.Fatal(e)
	}
	t.Cleanup(func() { s.Close() })
	vs, e := Scan(context.Background(), s)
	if e != nil || len(vs) != 1 || vs[0].Error != "" {
		t.Fatalf("scan %v %+v", e, vs)
	}
	return s, vs[0]
}
func TestArchiveCancelResumeConflict(t *testing.T) {
	s, v := fixture(t, false)
	o := Options{Output: t.TempDir()}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, e := Export(ctx, s, v, o); e == nil {
		t.Fatal("cancel")
	}
	p, e := Export(context.Background(), s, v, o)
	if e != nil {
		t.Fatal(e)
	}
	b, e := os.ReadFile(filepath.Join(p, "source", "manifest.json"))
	if e != nil || string(b) != string(v.Raw) {
		t.Fatal("manifest bytes")
	}
	if _, e = Export(context.Background(), s, v, o); e == nil {
		t.Fatal("overwrite")
	}
}
func TestBadManifestAndHash(t *testing.T) {
	s, v := fixture(t, false)
	v.Artifacts[0].SHA = strings.Repeat("0", 64)
	if _, e := Export(context.Background(), s, v, Options{Output: t.TempDir()}); e == nil {
		t.Fatal("hash")
	}
	for _, raw := range []string{`{"schema":"unknown"}`, `{"schema":"ylx.device-session.v4","session_id":"../escape"}`, `{"schema":"ylx.device-session.v4","session_id":"ok","x":{"path":"../x","bytes":0,"sha256":"bad"}}`} {
		v.Raw = []byte(raw)
		if parse(&v) == "" {
			t.Fatal(raw)
		}
	}
}
func TestPlaybackProbe(t *testing.T) {
	s, v := fixture(t, true)
	p, e := Export(context.Background(), s, v, Options{Output: t.TempDir(), Playback: true})
	if e != nil {
		t.Fatal(e)
	}
	b, e := exec.Command("ffprobe", "-v", "error", "-show_streams", "-of", "json", filepath.Join(p, "playback.mp4")).Output()
	if e != nil {
		t.Fatal(e)
	}
	var probe struct {
		Streams []struct {
			CodecType     string `json:"codec_type"`
			Width, Height int
			Frames        string `json:"nb_frames"`
		}
	}
	if e = json.Unmarshal(b, &probe); e != nil {
		t.Fatal(e)
	}
	video, audio := false, false
	for _, s := range probe.Streams {
		if s.CodecType == "video" {
			video = s.Width == 192 && s.Height == 64 && s.Frames == "24"
		}
		if s.CodecType == "audio" {
			audio = true
		}
	}
	if !video || !audio {
		t.Fatalf("probe %s", b)
	}
}

func TestResumeValidatedFile(t *testing.T) {
	s, v := fixture(t, false)
	o := Options{Output: t.TempDir()}
	bad := v
	bad.Artifacts = append([]Artifact(nil), v.Artifacts...)
	bad.Artifacts[len(bad.Artifacts)-1].SHA = strings.Repeat("0", 64)
	if _, e := Export(context.Background(), s, bad, o); e == nil {
		t.Fatal("expected failure")
	}
	if _, e := Export(context.Background(), s, v, o); e != nil {
		t.Fatal(e)
	}
}
func TestPartialUnknownAndTargetSymlink(t *testing.T) {
	s, v := fixture(t, false)
	v.Manifest.Sealed = false
	var m map[string]any
	json.Unmarshal(v.Raw, &m)
	m["sealed"] = false
	// Keep the actual source manifest immutable; partial status derives from directory/seal evidence.
	v.Status = "未封存 / partial"
	p, e := Export(context.Background(), s, v, Options{Output: t.TempDir()})
	if e != nil {
		t.Fatal(e)
	}
	b, _ := os.ReadFile(filepath.Join(p, "receipt.json"))
	if !strings.Contains(string(b), "partial") {
		t.Fatal(string(b))
	}
	d := t.TempDir()
	os.Symlink(t.TempDir(), filepath.Join(d, ".fixture-session-archive.partial"))
	if _, e = Export(context.Background(), s, v, Options{Output: d}); e == nil {
		t.Fatal("symlink staging")
	}
}

func TestSidecarsAndNamespace(t *testing.T) {
	s, v := fixture(t, false)
	d := t.TempDir()
	for _, a := range v.Artifacts {
		r, e := s.Open(a.Path)
		if e != nil {
			t.Fatal(e)
		}
		b, _ := io.ReadAll(r)
		r.Close()
		os.WriteFile(filepath.Join(d, a.Path), b, 0600)
	}
	os.WriteFile(filepath.Join(d, "manifest.json"), v.Raw, 0600)
	os.WriteFile(filepath.Join(d, "receipt.json"), []byte("original sidecar"), 0600)
	os.WriteFile(filepath.Join(d, "qa-note.txt"), []byte("qa"), 0600)
	src, e := source.NewFolder(d)
	if e != nil {
		t.Fatal(e)
	}
	defer src.Close()
	p, e := Export(context.Background(), src, v, Options{Output: t.TempDir()})
	if e != nil {
		t.Fatal(e)
	}
	b, e := os.ReadFile(filepath.Join(p, "source", "receipt.json"))
	if e != nil || string(b) != "original sidecar" {
		t.Fatal("sidecar missing")
	}
	b, _ = os.ReadFile(filepath.Join(p, "receipt.json"))
	if !strings.Contains(string(b), "qa-note.txt") {
		t.Fatal("no sidecar receipt")
	}
}

func TestIndependentAudioChunksAndRenderRetry(t *testing.T) {
	s, v := fixture(t, true)
	d := t.TempDir()
	for _, a := range v.Artifacts {
		r, e := s.Open(a.Path)
		if e != nil {
			t.Fatal(e)
		}
		b, _ := io.ReadAll(r)
		r.Close()
		os.WriteFile(filepath.Join(d, a.Path), b, 0600)
	}
	var m map[string]any
	json.Unmarshal(v.Raw, &m)
	var chunks []any
	for i := 0; i < 2; i++ {
		name := fmt.Sprintf("audio-%d.flac", i)
		b, e := exec.Command("ffmpeg", "-v", "error", "-i", filepath.Join(d, "audio.flac"), "-ss", fmt.Sprint(float64(i)/2), "-t", "0.5", filepath.Join(d, name)).CombinedOutput()
		if e != nil {
			t.Fatalf("%v %s", e, b)
		}
		data, _ := os.ReadFile(filepath.Join(d, name))
		hash := sha256.Sum256(data)
		chunks = append(chunks, map[string]any{"index": i, "start_time_seconds": float64(i) / 2, "end_time_seconds": float64(i+1) / 2, "artifact": map[string]any{"path": name, "bytes": len(data), "sha256": hex.EncodeToString(hash[:])}})
	}
	m["audio"] = map[string]any{"segments": chunks}
	raw, _ := json.Marshal(m)
	os.WriteFile(filepath.Join(d, "manifest.json"), raw, 0600)
	src, e := source.NewFolder(d)
	if e != nil {
		t.Fatal(e)
	}
	defer src.Close()
	vs, e := Scan(context.Background(), src)
	if e != nil {
		t.Fatal(e)
	}
	o := Options{Output: t.TempDir(), Playback: true}
	ctx, cancel := context.WithCancel(context.Background())
	o.Progress = func(p Progress) {
		if p.Stage == "复核目标" {
			cancel()
		}
	}
	if _, e = Export(ctx, src, vs[0], o); e == nil {
		t.Fatal("cancel expected")
	}
	stage := filepath.Join(o.Output, ".fixture-session-playback.partial")
	os.MkdirAll(filepath.Join(stage, "work"), 0700)
	os.WriteFile(filepath.Join(stage, "work", "playback.mp4"), []byte("cancelled renderer"), 0600)
	o.Progress = nil
	if _, e = Export(context.Background(), src, vs[0], o); e != nil {
		t.Fatal(e)
	}
}

func TestExportRejectsContainedBeforeMkdir(t *testing.T) {
	d := t.TempDir()
	s, e := source.NewFolder(d)
	if e != nil {
		t.Fatal(e)
	}
	defer s.Close()
	out := filepath.Join(d, "recordings", "id", "exports")
	if _, e = Export(context.Background(), s, Session{ID: "id"}, Options{Output: out}); e == nil {
		t.Fatal("contained accepted")
	}
	ds, _ := os.ReadDir(d)
	if len(ds) != 0 {
		t.Fatal("source modified")
	}
}
func TestOrderedRequiresContinuousIndices(t *testing.T) {
	if _, e := ordered([]Segment{{Index: 0, Start: 0, End: 1}, {Index: 2, Start: 1, End: 2}}); e == nil {
		t.Fatal("gap accepted")
	}
}
func TestPositiveAudioOffsetBoundedToVideo(t *testing.T) {
	s, v := fixture(t, true)
	d := t.TempDir()
	os.Mkdir(filepath.Join(d, "source"), 0700)
	for _, a := range v.Artifacts {
		r, e := s.Open(a.Path)
		if e != nil {
			t.Fatal(e)
		}
		b, _ := io.ReadAll(r)
		r.Close()
		os.WriteFile(filepath.Join(d, "source", a.Path), b, 0600)
	}
	seg := v.Manifest.Video.Segments[0]
	seg2 := seg
	seg2.Index = 1
	seg2.Start = 1
	seg2.End = 2
	v.Manifest.Video.Segments = append(v.Manifest.Video.Segments, seg2)
	audio := v.Manifest.Audio.Segments[0]
	audio.Start = .25
	audio.End = 2.25
	audio.Artifact.Path = "long.flac"
	b, e := exec.Command("ffmpeg", "-v", "error", "-stream_loop", "1", "-i", filepath.Join(d, "source", "audio.flac"), "-t", "2", filepath.Join(d, "source", "long.flac")).CombinedOutput()
	if e != nil {
		t.Fatalf("%v %s", e, b)
	}
	v.Manifest.Audio.Segments = []Segment{audio}
	if e = render(context.Background(), d, v, false, nil); e != nil {
		t.Fatal(e)
	}
	p, e := probe(context.Background(), filepath.Join(d, "playback.mp4"))
	if e != nil {
		t.Fatal(e)
	}
	for _, stream := range p.Streams {
		duration, e := strconv.ParseFloat(stream.Duration, 64)
		if e != nil || math.Abs(duration-2) > .025 {
			t.Fatalf("duration %s", stream.Duration)
		}
		if stream.CodecType == "video" && stream.Frames != "48" {
			t.Fatal(stream.Frames)
		}
	}
}
func TestPerSegmentEyeFrames(t *testing.T) {
	s, v := fixture(t, true)
	d := t.TempDir()
	os.Mkdir(filepath.Join(d, "source"), 0700)
	for _, a := range v.Artifacts {
		r, e := s.Open(a.Path)
		if e != nil {
			t.Fatal(e)
		}
		b, _ := io.ReadAll(r)
		r.Close()
		os.WriteFile(filepath.Join(d, "source", a.Path), b, 0600)
	}
	for _, n := range []int{12, 36} {
		p := filepath.Join(d, "source", fmt.Sprintf("right%d.mp4", n))
		b, e := exec.Command("ffmpeg", "-v", "error", "-stream_loop", "1", "-i", filepath.Join(d, "source", "right.mp4"), "-frames:v", fmt.Sprint(n), p).CombinedOutput()
		if e != nil {
			t.Fatalf("%v %s", e, b)
		}
	}
	a := v.Manifest.Video.Segments[0]
	a.Artifacts.Right.Path = "right12.mp4"
	b := a
	b.Index = 1
	b.Start = 1
	b.End = 2
	b.Artifacts.Right.Path = "right36.mp4"
	v.Manifest.Video.Segments = []Segment{a, b}
	e := render(context.Background(), d, v, false, nil)
	if e == nil || !strings.Contains(e.Error(), "视频段 0") {
		t.Fatalf("%v", e)
	}
}

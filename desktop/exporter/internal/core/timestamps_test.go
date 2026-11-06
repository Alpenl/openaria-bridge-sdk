package core

import (
	"encoding/json"
	"fmt"
	"openaria-exporter/internal/source"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
)

func TestStereoTimestampPrecision(t *testing.T) {
	requireMediaTools(t)
	dir := t.TempDir()
	for _, eye := range []string{"left", "right"} {
		expr := "N/(24*TB)"
		if eye == "right" {
			expr += "+if(mod(N\\,2)\\,1000\\,0)"
		}
		cmd := exec.CommandContext(t.Context(), "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=96x64:r=24:d=2", "-vf", "settb=1/1000000,setpts="+expr, "-fps_mode", "passthrough", "-enc_time_base", "1:1000000", "-c:v", "libx264", "-video_track_timescale", "1000000", filepath.Join(dir, eye+".mp4"))
		if b, e := cmd.CombinedOutput(); e != nil {
			t.Fatalf("fixture: %v %s", e, b)
		}
	}
	// This fixture specifically triggers the original failure with default encoder TB.
	old := exec.CommandContext(t.Context(), "ffmpeg", "-v", "error", "-xerror", "-i", filepath.Join(dir, "left.mp4"), "-i", filepath.Join(dir, "right.mp4"), "-filter_complex", "[0:v]setpts=PTS-STARTPTS[l];[1:v]setpts=PTS-STARTPTS[r];[l][r]hstack=inputs=2:shortest=1[v]", "-map", "[v]", "-fps_mode", "passthrough", "-c:v", "libx264", filepath.Join(t.TempDir(), "old.mp4"))
	b, err := old.CombinedOutput()
	if err == nil || !strings.Contains(string(b), "Non-monotonic DTS") {
		t.Fatalf("fixture no longer reproduces original: %v %s", err, b)
	}
	s, err := source.NewFolder(dir)
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
	var v Session
	v.ID = "timestamp-fixture"
	v.Dir = "."
	v.Raw = []byte(`{"test":"stereo-timestamp"}`)
	seg := Segment{Index: 0, Start: 0, End: 2}
	for _, eye := range []string{"left", "right"} {
		info, err := os.Stat(filepath.Join(dir, eye+".mp4"))
		if err != nil {
			t.Fatal(err)
		}
		a := Artifact{Path: eye + ".mp4", Bytes: info.Size()}
		v.Artifacts = append(v.Artifacts, a)
		if eye == "left" {
			seg.Artifacts.Left = a
		} else {
			seg.Artifacts.Right = a
		}
	}
	for _, count := range []int{1, 2} {
		t.Run(fmt.Sprint(count), func(t *testing.T) {
			v.Manifest.Video.Segments = nil
			for i := 0; i < count; i++ {
				x := seg
				x.Index = i
				x.Start = float64(i * 2)
				x.End = x.Start + 2
				v.Manifest.Video.Segments = append(v.Manifest.Video.Segments, x)
			}
			dst, err := Export(t.Context(), s, v, Options{Output: t.TempDir(), VideoOnly: true})
			if err != nil {
				t.Fatal(err)
			}
			video := filepath.Join(dst, "video.mp4")
			b, err := exec.CommandContext(t.Context(), "ffprobe", "-v", "error", "-show_packets", "-select_streams", "v:0", "-show_entries", "packet=dts", "-of", "json", video).Output()
			if err != nil {
				t.Fatal(err)
			}
			var packets struct {
				Packets []struct {
					DTS int64 `json:"dts"`
				} `json:"packets"`
			}
			if err = json.Unmarshal(b, &packets); err != nil {
				t.Fatal(err)
			}
			// Independently count frames emitted by framesync, without an H.264
			// encoder or MP4 muxer. The shorter eye clips the final update;
			// concat also preserves each eye's slightly different segment length.
			var list strings.Builder
			for range count {
				fmt.Fprintf(&list, "file '%s'\n", filepath.ToSlash(filepath.Join(dir, "left.mp4")))
			}
			leftList := filepath.Join(t.TempDir(), "left.txt")
			if err := os.WriteFile(leftList, []byte(list.String()), 0600); err != nil {
				t.Fatal(err)
			}
			rightList := filepath.Join(t.TempDir(), "right.txt")
			if err := os.WriteFile(rightList, []byte(strings.ReplaceAll(list.String(), "left.mp4", "right.mp4")), 0600); err != nil {
				t.Fatal(err)
			}
			ref := exec.CommandContext(t.Context(), "ffmpeg", "-v", "error", "-f", "concat", "-safe", "0", "-i", leftList, "-f", "concat", "-safe", "0", "-i", rightList, "-filter_complex", "[0:v]setpts=PTS-STARTPTS[l];[1:v]setpts=PTS-STARTPTS[r];[l][r]hstack=inputs=2:shortest=1[v]", "-map", "[v]", "-fps_mode", "passthrough", "-enc_time_base:v", "filter", "-f", "framemd5", "-")
			frames, err := ref.Output()
			if err != nil {
				t.Fatal(err)
			}
			want := 0
			for _, line := range strings.Split(string(frames), "\n") {
				if strings.HasPrefix(line, "0,") {
					want++
				}
			}
			if want < 48*count || len(packets.Packets) != want {
				t.Fatalf("lost frames: got %d want %d", len(packets.Packets), want)
			}
			for i := 1; i < len(packets.Packets); i++ {
				if packets.Packets[i].DTS <= packets.Packets[i-1].DTS {
					t.Fatal("non-increasing DTS")
				}
			}
			info, err := quickProbe(t.Context(), video)
			if err != nil {
				t.Fatal(err)
			}
			duration, err := strconv.ParseFloat(info.Format.Duration, 64)
			if err != nil || duration < float64(count*2)-.1 || duration > float64(count*2)+.1 {
				t.Fatalf("wrong duration %v %v", duration, err)
			}
		})
	}
}

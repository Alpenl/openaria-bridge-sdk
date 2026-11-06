package core

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"math"
	"openaria-exporter/internal/process"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"
)

type tail struct{ b []byte }

func (t *tail) Write(b []byte) (int, error) {
	n := len(b)
	if len(b) > 8192 {
		b = b[len(b)-8192:]
	}
	t.b = append(t.b, b...)
	if len(t.b) > 8192 {
		t.b = append([]byte(nil), t.b[len(t.b)-8192:]...)
	}
	return n, nil
}
func mediaTool(name string) (string, error) {
	exe, _ := os.Executable()
	candidates := []string{filepath.Join(os.Getenv("OPENARIA_FFMPEG_DIR"), name), filepath.Join(filepath.Dir(exe), name), filepath.Join(filepath.Dir(exe), "..", "Resources", name)}
	if os.Getenv("OPENARIA_FFMPEG_DIR") == "" {
		candidates = candidates[1:]
	}
	if p, e := exec.LookPath(name); e == nil {
		candidates = append(candidates, p)
	}
	candidates = append(candidates, "/opt/homebrew/bin/"+name, "/usr/local/bin/"+name)
	for _, p := range candidates {
		if s, e := os.Stat(p); e == nil && !s.IsDir() {
			return p, nil
		}
		if s, e := os.Stat(p + ".exe"); e == nil && !s.IsDir() {
			return p + ".exe", nil
		}
	}
	return "", fmt.Errorf("需要 %s：请安装 FFmpeg 或设置 OPENARIA_FFMPEG_DIR", name)
}

type mediaInfo struct {
	Streams []struct {
		CodecType     string `json:"codec_type"`
		Width, Height int
		Frames        string `json:"nb_read_frames"`
		Duration      string `json:"duration"`
	} `json:"streams"`
}

func probe(ctx context.Context, p string) (mediaInfo, error) {
	var info mediaInfo
	tool, e := mediaTool("ffprobe")
	if e != nil {
		return info, e
	}
	cmd := process.CommandContext(ctx, tool, "-v", "error", "-count_frames", "-show_entries", "stream=codec_type,width,height,nb_read_frames,duration", "-of", "json", p)
	b, e := cmd.Output()
	if e != nil {
		return info, fmt.Errorf("ffprobe %s: %w", p, e)
	}
	e = json.Unmarshal(b, &info)
	return info, e
}
func ordered(ss []Segment) ([]Segment, error) {
	v := append([]Segment(nil), ss...)
	sort.Slice(v, func(i, j int) bool { return v[i].Index < v[j].Index })
	for i, s := range v {
		if s.End <= s.Start || math.IsNaN(s.Start) || math.IsInf(s.End, 0) {
			return nil, fmt.Errorf("无效媒体时间")
		}
		if i > 0 && (s.Index != v[i-1].Index+1 || math.Abs(s.Start-v[i-1].End) > 0.001) {
			return nil, fmt.Errorf("不支持流内时间间隙/重叠")
		}
	}
	return v, nil
}
func render(ctx context.Context, dir string, v Session, hw bool, progress func(Progress), timings ...*sessionTimings) error {
	timing := &sessionTimings{}
	if len(timings) > 0 {
		timing = timings[0]
	}
	abs, err := filepath.Abs(dir)
	if err != nil {
		return err
	}
	dir = abs
	ff, e := mediaTool("ffmpeg")
	if e != nil {
		return e
	}
	if e = requireMediaFeatures(ctx, ff); e != nil {
		return e
	}
	video, e := ordered(v.Manifest.Video.Segments)
	if e != nil {
		return e
	}
	audio, e := ordered(v.Manifest.Audio.Segments)
	if e != nil {
		return e
	}
	if len(video) == 0 {
		return fmt.Errorf("没有视频")
	}
	work := filepath.Join(dir, "work")
	sourceDir := filepath.Join(dir, "source")
	if e = os.MkdirAll(work, 0700); e != nil {
		return e
	}
	lists := [][]string{{}, {}, {}}
	var frames int64
	var rightFrames int64
	var duration float64
	var width, height int
	for _, seg := range video {
		var eyeFrames [2]int64
		for eye, a := range []Artifact{seg.Artifacts.Left, seg.Artifacts.Right} {
			p := filepath.Join(sourceDir, filepath.FromSlash(a.Path))
			began := time.Now()
			info, e := probe(ctx, p)
			timing.inputProbe += time.Since(began)
			if e != nil {
				return e
			}
			valid := false
			for _, s := range info.Streams {
				if s.CodecType == "video" {
					n, e := strconv.ParseInt(s.Frames, 10, 64)
					if e != nil {
						return fmt.Errorf("无法核实源帧数")
					}
					eyeFrames[eye] = n
					if eye == 0 {
						dur, err := strconv.ParseFloat(s.Duration, 64)
						if err != nil || dur <= 0 || math.IsInf(dur, 0) || math.IsNaN(dur) {
							return fmt.Errorf("无法核实实际视频时长")
						}
						duration += dur
						frames += n
					} else {
						rightFrames += n
					}
					if width == 0 {
						width, height = s.Width, s.Height
					}
					if width != s.Width || height != s.Height {
						return fmt.Errorf("分段分辨率不一致")
					}
					valid = true
				}
			}
			if !valid {
				return fmt.Errorf("没有视频流")
			}
			lists[eye] = append(lists[eye], p)
		}
		if eyeFrames[0] != eyeFrames[1] {
			return fmt.Errorf("视频段 %d 左右眼帧数不同", seg.Index)
		}
	}
	for _, seg := range audio {
		lists[2] = append(lists[2], filepath.Join(sourceDir, filepath.FromSlash(seg.Artifact.Path)))
	}
	if frames != rightFrames {
		return fmt.Errorf("左右眼帧数不同，拒绝丢帧拼接")
	}
	args := []string{"-nostdin", "-v", "error", "-y", "-progress", "pipe:1"}
	for i, list := range lists {
		if len(list) == 0 {
			continue
		}
		var b strings.Builder
		for _, p := range list {
			if strings.ContainsAny(p, "\n\r") {
				return fmt.Errorf("媒体路径含换行")
			}
			fmt.Fprintf(&b, "file '%s'\n", strings.ReplaceAll(filepath.ToSlash(p), "'", "'\\''"))
		}
		p := filepath.Join(work, fmt.Sprintf("stream-%d.txt", i))
		if e = os.WriteFile(p, []byte(b.String()), 0600); e != nil {
			return e
		}
		args = append(args, "-f", "concat", "-safe", "0", "-i", p)
	}
	filter := "[0:v]setpts=PTS-STARTPTS[l];[1:v]setpts=PTS-STARTPTS[r];[l][r]hstack=inputs=2[v]"
	if len(audio) > 0 {
		offset := audio[0].Start - video[0].Start
		if offset >= 0 {
			filter += fmt.Sprintf(";[2:a]asetpts=PTS-STARTPTS,adelay=%d:all=1,apad,atrim=duration=%.9f[a]", int64(math.Round(offset*1000)), duration)
		} else {
			filter += fmt.Sprintf(";[2:a]atrim=start=%.9f,asetpts=PTS-STARTPTS,apad,atrim=duration=%.9f[a]", -offset, duration)
		}
	}
	args = append(args, "-filter_complex", filter, "-map", "[v]", "-fps_mode", "passthrough", "-enc_time_base:v", "filter")
	if len(audio) > 0 {
		args = append(args, "-map", "[a]", "-c:a", "aac")
	}
	codec := "libx264"
	if hw {
		codec = "h264_videotoolbox"
	}
	tmp := filepath.Join(work, "playback.mp4")
	args = append(args, "-c:v", codec)
	if hw {
		var videoBytes int64
		for _, seg := range video {
			videoBytes += seg.Artifacts.Left.Bytes + seg.Artifacts.Right.Bytes
		}
		bitrate := max(int64(8_000_000), int64(float64(videoBytes)*8/duration*1.25))
		args = append(args, "-b:v", strconv.FormatInt(bitrate, 10))
	} else {
		args = append(args, "-preset", "fast", "-crf", "18")
	}
	args = append(args, "-pix_fmt", "yuv420p", "-movflags", "+faststart", tmp)
	cmd := process.CommandContext(ctx, ff, args...)
	errout := &tail{}
	cmd.Stderr = errout
	pipe, e := cmd.StdoutPipe()
	if e != nil {
		return e
	}
	encodeStart := time.Now()
	if e = cmd.Start(); e != nil {
		return e
	}
	scanner := bufio.NewScanner(pipe)
	last := time.Time{}
	for scanner.Scan() {
		line := scanner.Text()
		if strings.HasPrefix(line, "out_time=") && progress != nil && time.Since(last) > 150*time.Millisecond {
			last = time.Now()
			progress(Progress{File: strings.TrimPrefix(line, "out_time="), Stage: "合并编码"})
		}
	}
	e = cmd.Wait()
	timing.encode += time.Since(encodeStart)
	if e != nil {
		return fmt.Errorf("FFmpeg: %w: %s", e, errout.b)
	}
	if e = scanner.Err(); e != nil {
		return e
	}
	probeStart := time.Now()
	info, e := probe(ctx, tmp)
	timing.outputProbe += time.Since(probeStart)
	if e != nil {
		return e
	}
	hasVideo, hasAudio := false, false
	for _, s := range info.Streams {
		if s.CodecType == "video" {
			n, _ := strconv.ParseInt(s.Frames, 10, 64)
			hasVideo = s.Width == width*2 && s.Height == height && n == frames
		}
		if s.CodecType == "audio" {
			hasAudio = true
		}
	}
	if !hasVideo || len(audio) > 0 && !hasAudio {
		return fmt.Errorf("输出 ffprobe 帧数/尺寸/音轨验证失败")
	}
	return os.Rename(tmp, filepath.Join(dir, "playback.mp4"))
}

package core

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"log"
	"math"
	"openaria-exporter/internal/process"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

type headerInfo struct {
	Streams []struct {
		Type          string `json:"codec_type"`
		Codec         string `json:"codec_name"`
		Width, Height int
		Duration      string `json:"duration"`
		TimeBase      string `json:"time_base"`
		FrameRate     string `json:"avg_frame_rate"`
	} `json:"streams"`
	Format struct {
		Duration string `json:"duration"`
	} `json:"format"`
}

func mediaEnvironment() []string {
	var out []string
	for _, s := range os.Environ() {
		key, _, _ := strings.Cut(s, "=")
		switch strings.ToLower(key) {
		case "http_proxy", "https_proxy", "all_proxy":
			continue
		}
		out = append(out, s)
	}
	return out
}
func quickProbe(ctx context.Context, p string) (headerInfo, error) {
	var info headerInfo
	ff, e := mediaTool("ffprobe")
	if e != nil {
		return info, e
	}
	cmd := process.CommandContext(ctx, ff, "-v", "error", "-show_entries", "stream=codec_type,codec_name,width,height,duration,time_base,avg_frame_rate:format=duration", "-of", "json", p)
	cmd.Env = mediaEnvironment()
	errout := &tail{}
	cmd.Stderr = errout
	b, e := cmd.Output()
	if e != nil {
		return info, fmt.Errorf("读取媒体头：%w：%s", e, errout.b)
	}
	e = json.Unmarshal(b, &info)
	return info, e
}
func videoShape(info headerInfo) (int, int, string) {
	for _, s := range info.Streams {
		if s.Type == "video" {
			return s.Width, s.Height, s.Codec
		}
	}
	return 0, 0, ""
}
func mediaDuration(v Session) float64 {
	var d float64
	for _, s := range v.Manifest.Video.Segments {
		d += s.End - s.Start
	}
	return d
}
func mediaBitrate(v Session) int64 {
	var n int64
	for _, s := range v.Manifest.Video.Segments {
		n += s.Artifacts.Left.Bytes + s.Artifacts.Right.Bytes
	}
	d := mediaDuration(v)
	if d <= 0 {
		return 8_000_000
	}
	return max(8_000_000, int64(float64(n)*8/d*1.25))
}
func renderStreaming(ctx context.Context, dir string, v Session, g *mediaStream, progress func(Progress), timing *sessionTimings) error {
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
	dir, e = filepath.Abs(dir)
	if e != nil {
		return e
	}
	work := filepath.Join(dir, "work")
	start := time.Now()
	left, e := quickProbe(ctx, g.URL(v.Dir, video[0].Artifacts.Left.Path))
	if e != nil {
		return e
	}
	right, e := quickProbe(ctx, g.URL(v.Dir, video[0].Artifacts.Right.Path))
	if e != nil {
		return e
	}
	timing.inputProbe += time.Since(start)
	log.Printf("media headers session=%s segments=%d left=%+v right=%+v", v.ID, len(video), left, right)
	width, height, codec := videoShape(left)
	rw, rh, _ := videoShape(right)
	if width <= 0 || height <= 0 || width != rw || height != rh {
		return fmt.Errorf("左右眼分辨率无效或不一致")
	}
	duration := mediaDuration(v)
	if duration <= 0 || math.IsNaN(duration) || math.IsInf(duration, 0) {
		return fmt.Errorf("无效录像时长")
	}
	lists := []strings.Builder{{}, {}, {}}
	for _, seg := range video {
		for i, a := range []Artifact{seg.Artifacts.Left, seg.Artifacts.Right} {
			fmt.Fprintf(&lists[i], "file '%s'\n", g.URL(v.Dir, a.Path))
		}
	}
	for _, seg := range audio {
		fmt.Fprintf(&lists[2], "file '%s'\n", g.URL(v.Dir, seg.Artifact.Path))
	}
	var inputs []string
	for i := range lists {
		if lists[i].Len() == 0 {
			continue
		}
		p := filepath.Join(work, fmt.Sprintf("stream-%d.txt", i))
		if e = os.WriteFile(p, []byte(lists[i].String()), 0600); e != nil {
			return e
		}
		inputs = append(inputs, p)
	}
	profile, e := chooseEncoder(ctx, ff)
	if e != nil {
		return e
	}
	decodeHardware := profile.useHardwareDecode(codec, width, height)
	// Retry unavailable hardware without exposing device-specific UI options.
	type attempt struct {
		profile  encoderProfile
		decode   bool
		gpuStack bool
		repair   bool
	}
	var attempts []attempt
	if decodeHardware && profile.stack != "" && !g.stackFailed(stackFailureKey(profile, codec, width, height)) {
		attempts = append(attempts, attempt{profile: profile, decode: true, gpuStack: true})
	}
	attempts = append(attempts, attempt{profile: profile, decode: decodeHardware})
	if decodeHardware {
		attempts = append(attempts, attempt{profile: profile})
	}
	if profile.name != "libx264" {
		attempts = append(attempts, attempt{profile: softwareEncoder()})
	}
	attempts = append(attempts, attempt{profile: softwareEncoder(), repair: true})
	tmp := filepath.Join(work, "playback.mp4")
	needsRepair := false
	var audited [2]timelineAudit
	repaired := false
	for i, a := range attempts {
		if needsRepair && !a.repair {
			continue
		}
		if a.repair && !needsRepair {
			break
		}
		if a.repair {
			for eye := 0; eye < 2; eye++ {
				audit, err := auditTimeline(ctx, inputs[eye])
				if err != nil {
					return fmt.Errorf("时间轴安全预检失败（眼 %d）：%w；原编码错误：%v", eye, err, e)
				}
				if audit.width != width || audit.height != height {
					return fmt.Errorf("时间轴预检左右眼尺寸不一致")
				}
				audited[eye] = audit
				log.Printf("timeline audit session=%s eye=%d frames=%d duration=%.6f correction=%.9f", v.ID, eye, audit.frames, audit.previous-audit.first, audit.correction)
			}
			if math.Abs((audited[0].adjusted-audited[0].first)-(audited[1].adjusted-audited[1].first)) > .15 {
				return fmt.Errorf("左右眼时长差超过 150ms，拒绝静默截断")
			}
		}
		if e = ctx.Err(); e != nil {
			return e
		}
		if e = g.Err(); e != nil {
			return e
		}
		filter := "[0:v]setpts=PTS-STARTPTS[l];[1:v]setpts=PTS-STARTPTS[r];[l][r]hstack=inputs=2:shortest=1[v]"
		if a.gpuStack {
			filter = strings.Replace(filter, "hstack=", ""+a.profile.stack+"=", 1)
		}
		if a.repair {
			filter = strings.ReplaceAll(filter, "setpts=PTS-STARTPTS", "setpts=PTS-STARTPTS,"+timelineFilter)
			filter = strings.TrimSuffix(filter, "[v]") + "," + timelineFilter + "[v]"
		}
		mapped := "[v]"
		if a.profile.upload && !a.gpuStack {
			filter += ";[v]format=nv12,hwupload[hw]"
			mapped = "[hw]"
		}
		if len(audio) > 0 {
			offset := audio[0].Start - video[0].Start
			if math.IsNaN(offset) || math.IsInf(offset, 0) || math.Abs(offset) > duration {
				return fmt.Errorf("音视频起点偏移超出录像时长，拒绝生成误同步成品")
			}
			if offset >= 0 {
				filter += fmt.Sprintf(";[2:a]asetpts=PTS-STARTPTS,adelay=%d:all=1,apad[a]", int64(math.Round(offset*1000)))
			} else {
				filter += fmt.Sprintf(";[2:a]atrim=start=%.9f,asetpts=PTS-STARTPTS,apad[a]", -offset)
			}
		}
		args := []string{"-nostdin", "-v", "error", "-xerror", "-y", "-progress", "pipe:1", "-filter_complex_threads", "2"}
		args = append(args, a.profile.global...)
		for n, p := range inputs {
			if a.decode && n < 2 {
				args = append(args, a.profile.decoderArgs(a.gpuStack)...)
			}
			args = append(args, "-threads", "2", "-protocol_whitelist", "file,http,tcp", "-f", "concat", "-safe", "0", "-i", p)
		}
		// Framesync can emit closely spaced frames from the two capture clocks.
		// Keep the filter time base: the default 1/framerate encoder time base
		// rounds distinct timestamps together, causing duplicate DTS in MP4.
		args = append(args, "-filter_complex", filter, "-map", mapped, "-fps_mode", "passthrough", "-enc_time_base:v", "filter")
		if len(audio) > 0 {
			args = append(args, "-map", "[a]", "-c:a", "aac", "-shortest", "-shortest_buf_duration", "0.1")
		}
		args = append(args, a.profile.args(mediaBitrate(v))...)
		if a.repair {
			args = append(args, "-bf", "0", "-video_track_timescale", "1000000")
		}
		if !a.profile.upload && !a.gpuStack {
			args = append(args, "-pix_fmt", "nv12")
		}
		if start, ok := recordingDate(sessionTime(v).StartedAt); ok {
			args = append(args, "-metadata", "creation_time="+start.UTC().Format(time.RFC3339Nano))
		}
		// Local playback does not need faststart's final full-file relocation pass.
		args = append(args, tmp)
		log.Printf("stream encode session=%s encoder=%s hardware_decode=%v hardware_stack=%v timeline_repair=%v attempt=%d", v.ID, a.profile.name, a.decode, a.gpuStack, a.repair, i+1)
		if progress != nil {
			progress(Progress{Stage: "边读边编码", File: "准备媒体"})
		}
		start = time.Now()
		e = runMedia(ctx, ff, args, progress)
		timing.encode += time.Since(start)
		if e == nil {
			repaired = a.repair
			break
		}
		log.Printf("stream encode fallback session=%s reason=%v", v.ID, e)
		if ctx.Err() != nil {
			return ctx.Err()
		}
		if err := g.Err(); err != nil {
			return err
		}
		if a.gpuStack {
			g.recordStackFailure(stackFailureKey(a.profile, codec, width, height), e)
		}
		needsRepair = needsRepair || timestampFailure(e)
	}
	if e != nil {
		return e
	}
	if e = g.Err(); e != nil {
		return e
	}
	start = time.Now()
	info, e := quickProbe(ctx, tmp)
	timing.outputProbe += time.Since(start)
	if e != nil {
		return e
	}
	w, h, _ := videoShape(info)
	if w != width*2 || h != height {
		return fmt.Errorf("输出分辨率异常")
	}
	actual, e := strconv.ParseFloat(info.Format.Duration, 64)
	if e != nil || math.IsNaN(actual) || math.IsInf(actual, 0) || actual <= 0 {
		return fmt.Errorf("输出时长无效：%s", info.Format.Duration)
	}
	if repaired {
		expected := math.Min(audited[0].adjusted-audited[0].first, audited[1].adjusted-audited[1].first)
		if actual < expected-.05 || actual > expected+1 {
			return fmt.Errorf("修复后视频时长异常：实际 %.6f，预检 %.6f", actual, expected)
		}
		if err := verifyRepairedPackets(ctx, tmp); err != nil {
			return err
		}
		timing.timelineRepaired = true
		log.Printf("timeline recovery verified session=%s duration=%.6f", v.ID, actual)
	}
	// The muxer stops at the decoded video's end. Capture clock timestamps in
	// the manifest can differ from actual media duration; never pad to those.
	for _, s := range info.Streams {
		if s.Type == "video" {
			d, err := strconv.ParseFloat(s.Duration, 64)
			if err != nil || math.IsNaN(d) || math.IsInf(d, 0) || d <= 0 || math.Abs(actual-d) > 0.15 {
				return fmt.Errorf("输出音视频结束时间异常")
			}
		}
	}
	if len(audio) > 0 {
		found := false
		for _, s := range info.Streams {
			found = found || s.Type == "audio"
		}
		if !found {
			return fmt.Errorf("输出缺少音轨")
		}
	}
	return os.Rename(tmp, filepath.Join(dir, "playback.mp4"))
}
func runMedia(ctx context.Context, ff string, args []string, progress func(Progress)) error {
	cmd := process.CommandContext(ctx, ff, args...)
	cmd.Env = mediaEnvironment()
	errout := &tail{}
	cmd.Stderr = errout
	pipe, e := cmd.StdoutPipe()
	if e != nil {
		return e
	}
	if e = cmd.Start(); e != nil {
		return e
	}
	scanner := bufio.NewScanner(pipe)
	last := time.Time{}
	for scanner.Scan() {
		line := scanner.Text()
		if strings.HasPrefix(line, "out_time=") && progress != nil && time.Since(last) > 150*time.Millisecond {
			last = time.Now()
			progress(Progress{Stage: "边读边编码", File: strings.TrimPrefix(line, "out_time=")})
		}
	}
	e = cmd.Wait()
	if e != nil {
		return fmt.Errorf("FFmpeg: %w: %s", e, errout.b)
	}
	return scanner.Err()
}

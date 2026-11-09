package core

import (
	"bufio"
	"context"
	"fmt"
	"math"
	"openaria-exporter/internal/process"
	"strconv"
	"strings"
)

// Recovery is deliberately narrow: at most 1 ms regression and 10 ms total
// adjustment per eye. Never flatten a clock reset or invent a frame rate.
const timelineFilter = "settb=1/1000000,setpts=if(isnan(PREV_OUTPTS)\\,PTS\\,max(PTS\\,PREV_OUTPTS+1))"

type timelineAudit struct {
	frames                                int64
	width, height                         int
	first, previous, adjusted, correction float64
}

func (a *timelineAudit) add(t float64) error {
	if math.IsNaN(t) || math.IsInf(t, 0) {
		return fmt.Errorf("缺失或无效视频时间戳")
	}
	if a.frames == 0 {
		a.first = t
		a.previous = t
		a.adjusted = t
		a.frames++
		return nil
	}
	if t < a.previous-.00100001 {
		return fmt.Errorf("视频时间戳倒退超过 1ms，拒绝猜测时间轴")
	}
	if t-a.previous > 10 {
		return fmt.Errorf("视频时间戳间隔超过 10s，拒绝猜测时间轴")
	}
	adjusted := math.Max(t, a.adjusted+.000001)
	a.correction += adjusted - t
	if a.correction > .01 {
		return fmt.Errorf("累计时间戳修正超过 10ms，保留原始文件")
	}
	a.previous = t
	a.adjusted = adjusted
	a.frames++
	return nil
}

// Validate recovery output without decoding or retaining a per-packet array.
func verifyRepairedPackets(ctx context.Context, file string) error {
	ff, err := mediaTool("ffprobe")
	if err != nil {
		return err
	}
	c, cancel := context.WithCancel(ctx)
	defer cancel()
	cmd := process.CommandContext(c, ff, "-v", "error", "-select_streams", "v:0", "-show_packets", "-show_entries", "packet=pts,dts", "-of", "compact=p=1:nk=0", file)
	cmd.Env = mediaEnvironment()
	stderr := &tail{}
	cmd.Stderr = stderr
	pipe, err := cmd.StdoutPipe()
	if err != nil {
		return err
	}
	if err = cmd.Start(); err != nil {
		return err
	}
	scan := bufio.NewScanner(pipe)
	var previous int64
	count := 0
	var checkErr error
	for scan.Scan() {
		line := scan.Text()
		if !strings.HasPrefix(line, "packet|") {
			continue
		}
		fields := map[string]string{}
		for _, f := range strings.Split(line, "|") {
			k, v, ok := strings.Cut(f, "=")
			if ok {
				fields[k] = v
			}
		}
		pts, pe := strconv.ParseInt(fields["pts"], 10, 64)
		dts, de := strconv.ParseInt(fields["dts"], 10, 64)
		if pe != nil || de != nil || pts != dts || (count > 0 && dts <= previous) {
			checkErr = fmt.Errorf("修复输出时间戳验证失败")
			cancel()
			break
		}
		previous = dts
		count++
	}
	if scan.Err() != nil {
		checkErr = scan.Err()
		cancel()
	}
	waitErr := cmd.Wait()
	if ctx.Err() != nil {
		return ctx.Err()
	}
	if checkErr != nil {
		return checkErr
	}
	if waitErr != nil || len(stderr.b) > 0 || count == 0 {
		return fmt.Errorf("修复输出包验证失败：%v %s packets=%d", waitErr, stderr.b, count)
	}
	return nil
}

func timestampFailure(err error) bool {
	if err == nil {
		return false
	}
	text := strings.ToLower(err.Error())
	return strings.Contains(text, "non-monotonic dts") || strings.Contains(text, "non monotonically increasing dts")
}

// Only the recovery path pays for a streaming decoded-timestamp scan. It keeps
// no per-frame collection and makes no hash pass over the source.
func auditTimeline(ctx context.Context, list string) (timelineAudit, error) {
	var audit timelineAudit
	ff, err := mediaTool("ffprobe")
	if err != nil {
		return audit, err
	}
	c, cancel := context.WithCancel(ctx)
	defer cancel()
	cmd := process.CommandContext(c, ff, "-v", "error", "-threads", "1", "-protocol_whitelist", "file,http,tcp", "-f", "concat", "-safe", "0", "-i", list, "-select_streams", "v:0", "-show_frames", "-show_entries", "frame=pts_time,width,height", "-of", "compact=p=1:nk=0")
	cmd.Env = mediaEnvironment()
	stderr := &tail{}
	cmd.Stderr = stderr
	pipe, err := cmd.StdoutPipe()
	if err != nil {
		return audit, err
	}
	if err = cmd.Start(); err != nil {
		return audit, err
	}
	scanner := bufio.NewScanner(pipe)
	var parseErr error
	for scanner.Scan() {
		line := scanner.Text()
		if line == "" {
			continue
		}
		// Side-data continuation lines do not represent additional video frames.
		if !strings.HasPrefix(line, "frame|") {
			continue
		}
		if !strings.Contains(line, "pts_time=") {
			parseErr = fmt.Errorf("视频帧缺少时间戳")
			cancel()
			break
		}
		fields := map[string]string{}
		for _, field := range strings.Split(line, "|") {
			k, v, ok := strings.Cut(field, "=")
			if ok {
				fields[k] = v
			}
		}
		w, we := strconv.Atoi(fields["width"])
		h, he := strconv.Atoi(fields["height"])
		if we != nil || he != nil || w <= 0 || h <= 0 || (audit.frames > 0 && (w != audit.width || h != audit.height)) {
			parseErr = fmt.Errorf("分段解码尺寸无效或发生变化，拒绝时间轴修复")
			cancel()
			break
		}
		audit.width, audit.height = w, h
		value := fields["pts_time"]
		t, e := strconv.ParseFloat(value, 64)
		if e != nil {
			parseErr = fmt.Errorf("缺失视频帧时间戳：%q", value)
		} else {
			parseErr = audit.add(t)
		}
		if parseErr != nil {
			cancel()
			break
		}
	}
	scanErr := scanner.Err()
	if scanErr != nil {
		cancel()
	}
	waitErr := cmd.Wait()
	if ctx.Err() != nil {
		return audit, ctx.Err()
	}
	if parseErr != nil {
		return audit, parseErr
	}
	if scanErr != nil {
		return audit, scanErr
	}
	if waitErr != nil || len(stderr.b) > 0 {
		return audit, fmt.Errorf("时间轴预检解码失败：%v %s", waitErr, stderr.b)
	}
	if audit.frames < 1 {
		return audit, fmt.Errorf("时间轴预检没有视频帧")
	}
	return audit, nil
}

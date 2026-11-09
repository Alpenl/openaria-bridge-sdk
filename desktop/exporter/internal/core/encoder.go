package core

import (
	"context"
	"fmt"
	"log"
	"openaria-exporter/internal/process"
	"os"
	"runtime"
	"strings"
	"sync"
	"time"
)

type encoderProfile struct {
	name          string
	global, flags []string
	upload        bool
	stack         string
}

func softwareEncoder() encoderProfile {
	// Input -threads does not limit the encoder. Bound frame threads and
	// lookahead explicitly, particularly for 3840x1080 stereo output.
	return encoderProfile{name: "libx264", flags: []string{"-preset", "veryfast", "-crf", "18", "-threads:v", "2", "-rc-lookahead", "8"}}
}

func lowMemoryEncoder() encoderProfile {
	// Avoid tune=zerolatency: its force-cfr setting can alter capture timing.
	return encoderProfile{name: "libx264", flags: []string{"-preset", "veryfast", "-crf", "18", "-threads:v", "1", "-rc-lookahead", "0", "-bf", "0", "-x264-params", "sync-lookahead=0"}}
}

func allocationFailure(err error) bool {
	if err == nil {
		return false
	}
	s := strings.ToLower(err.Error())
	return strings.Contains(s, "cannot allocate memory") ||
		strings.Contains(s, "error allocating the output packet") ||
		(strings.Contains(s, "x264 [error]: malloc of size ") && strings.Contains(s, " failed"))
}
func encoderCandidates(system string) []encoderProfile {
	switch system {
	case "darwin":
		return []encoderProfile{{name: "h264_videotoolbox", flags: []string{"-allow_sw", "0", "-prio_speed", "1"}}}
	case "windows":
		return []encoderProfile{{name: "h264_nvenc", flags: []string{"-preset", "p1"}}, {name: "h264_qsv", global: []string{"-init_hw_device", "qsv=hw", "-filter_hw_device", "hw"}, flags: []string{"-preset", "veryfast"}}, {name: "h264_amf", flags: []string{"-quality", "speed", "-usage", "transcoding"}}}
	default:
		out := []encoderProfile{{name: "h264_nvenc", flags: []string{"-preset", "p1"}}, {name: "h264_qsv", global: []string{"-init_hw_device", "qsv=hw", "-filter_hw_device", "hw"}, flags: []string{"-preset", "veryfast"}}}
		if ds, e := os.ReadDir("/dev/dri"); e == nil {
			for _, d := range ds {
				if strings.HasPrefix(d.Name(), "renderD") {
					out = append(out, encoderProfile{name: "h264_vaapi", global: []string{"-vaapi_device", "/dev/dri/" + d.Name()}, upload: true})
				}
			}
		}
		return out
	}
}

var encoderSelection struct {
	sync.Mutex
	path    string
	profile encoderProfile
	ready   bool
}

func chooseEncoder(ctx context.Context, ff string) (encoderProfile, error) {
	encoderSelection.Lock()
	defer encoderSelection.Unlock()
	if encoderSelection.ready && encoderSelection.path == ff {
		return encoderSelection.profile, nil
	}
	selected := softwareEncoder()
	best := time.Duration(1<<63 - 1)
	for _, p := range encoderCandidates(runtime.GOOS) {
		if e := ctx.Err(); e != nil {
			return selected, e
		}
		trial, cancel := context.WithTimeout(ctx, 8*time.Second)
		args := append([]string{"-nostdin", "-v", "error"}, p.global...)
		args = append(args, "-f", "lavfi", "-i", "color=c=gray:s=1280x720:r=30", "-frames:v", "60", "-an")
		if p.upload {
			args = append(args, "-vf", "format=nv12,hwupload")
		}
		args = append(args, "-c:v", p.name, "-b:v", "8000000")
		args = append(args, p.flags...)
		args = append(args, "-f", "null", "-")
		start := time.Now()
		output, e := process.CommandContext(trial, ff, args...).CombinedOutput()
		elapsed := time.Since(start)
		cancel()
		if e != nil {
			log.Printf("encoder unavailable name=%s device=%v reason=%v detail=%.500s", p.name, p.global, e, output)
			continue
		}
		log.Printf("encoder trial name=%s device=%v elapsed=%s", p.name, p.global, elapsed)
		if elapsed < best {
			best = elapsed
			selected = p
		}
	}
	if e := ctx.Err(); e != nil {
		return selected, e
	}
	if selected.name == "h264_qsv" || selected.name == "h264_vaapi" {
		filter := "hstack_" + strings.TrimPrefix(selected.name, "h264_")
		probe, cancel := context.WithTimeout(ctx, 3*time.Second)
		output, e := process.CommandContext(probe, ff, "-hide_banner", "-filters").Output()
		cancel()
		if e == nil {
			for _, line := range strings.Split(string(output), "\n") {
				fields := strings.Fields(line)
				if len(fields) > 1 && fields[1] == filter {
					selected.stack = filter
				}
			}
		}
	}
	log.Printf("automatic encoder=%s device=%v stack=%s", selected.name, selected.global, selected.stack)
	encoderSelection.path, encoderSelection.profile, encoderSelection.ready = ff, selected, true
	return selected, nil
}
func (p encoderProfile) useHardwareDecode(codec string, width, height int) bool {
	if codec != "h264" && codec != "hevc" {
		return false
	}
	// Apple Silicon experiments: 1080p H.264 decode + CPU stereo stacking
	// took 0.62s in software vs 2.15s with VideoToolbox frame downloads.
	// Keep the hardware encoder; reserve hardware decode for heavier formats.
	if runtime.GOOS == "darwin" && runtime.GOARCH == "arm64" && codec == "h264" && int64(width)*int64(height) <= 1920*1080 {
		return false
	}
	return len(p.decoderArgs(false)) > 0
}

func (p encoderProfile) decoderArgs(gpuStack bool) []string {
	accel := ""
	switch p.name {
	case "h264_videotoolbox":
		accel = "videotoolbox"
	case "h264_nvenc":
		accel = "cuda"
	case "h264_qsv":
		accel = "qsv"
	case "h264_vaapi":
		accel = "vaapi"
	case "h264_amf":
		if runtime.GOOS == "windows" {
			accel = "d3d11va"
		}
	}
	if accel == "" {
		return nil
	}
	out := []string{"-hwaccel", accel}
	if accel == "qsv" {
		out = append(out, "-hwaccel_device", "hw")
	}
	if gpuStack {
		out = append(out, "-hwaccel_output_format", accel)
	} else if accel != "videotoolbox" {
		out = append(out, "-hwaccel_output_format", "nv12")
	}
	return out
}

func (p encoderProfile) args(bitrate int64) []string {
	out := []string{"-c:v", p.name}
	out = append(out, p.flags...)
	if p.name != "libx264" {
		out = append(out, "-b:v", fmt.Sprint(bitrate))
	}
	return out
}

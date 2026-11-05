package core

import (
	"context"
	"fmt"
	"strings"
	"sync"
	"time"

	"openaria-exporter/internal/process"
)

var mediaFeatureCache sync.Map

// Ubuntu 22.04 ships FFmpeg 4.4. Its legacy -shortest behavior can stall with
// padded audio. Require the actual options used by our pipeline, rather than
// retrying encoders with an incompatible tool or guessing from version strings.
func requireMediaFeatures(ctx context.Context, ff string) error {
	if _, ok := mediaFeatureCache.Load(ff); ok {
		return nil
	}
	probe, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	b, err := process.CommandContext(probe, ff, "-hide_banner", "-h", "full").Output()
	if err != nil {
		return fmt.Errorf("读取 FFmpeg 参数支持：%w", err)
	}
	if !supportsMediaPipeline(string(b)) {
		return fmt.Errorf("FFmpeg 版本过旧或缺少必要参数，请使用附带媒体工具的完整安装包（需要 FFmpeg 6 或更新版本）：%s", ff)
	}
	mediaFeatureCache.Store(ff, true)
	return nil
}

func supportsMediaPipeline(help string) bool {
	fpsMode, shortestBuffer := false, false
	for _, line := range strings.Split(help, "\n") {
		fields := strings.Fields(line)
		if len(fields) == 0 {
			continue
		}
		switch strings.SplitN(fields[0], "[", 2)[0] {
		case "-fps_mode":
			fpsMode = true
		case "-shortest_buf_duration":
			shortestBuffer = true
		}
	}
	return fpsMode && shortestBuffer
}

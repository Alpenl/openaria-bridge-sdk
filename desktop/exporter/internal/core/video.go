package core

import (
	"context"
	"encoding/json"
	"os"
	"time"
)

func mediaArtifacts(v Session) map[string]Artifact {
	out := map[string]Artifact{}
	canonical := map[string]Artifact{}
	for _, a := range v.Artifacts {
		canonical[a.Path] = a
	}
	add := func(a Artifact) {
		if normalized, ok := canonical[a.Path]; ok {
			a = normalized
		}
		out[a.Path] = a
	}
	for _, seg := range v.Manifest.Video.Segments {
		add(seg.Artifacts.Left)
		add(seg.Artifacts.Right)
	}
	for _, seg := range v.Manifest.Audio.Segments {
		add(seg.Artifact)
	}
	return out
}
func MediaBytes(v Session) int64 {
	var n int64
	for _, a := range mediaArtifacts(v) {
		n += a.Bytes
	}
	return n
}

// ExportBytes estimates reads for encoding plus the retained recording copy.
func ExportBytes(v Session) int64 {
	return MediaBytes(v) + v.Bytes
}
func finishVideo(ctx context.Context, st *os.Root, v Session, manifestSHA string, repaired bool, files []Artifact) error {
	if e := st.Rename("playback.mp4", "video.mp4"); e != nil {
		return e
	}
	f, e := st.OpenFile("video.mp4", os.O_RDWR, 0)
	if e != nil {
		return e
	}
	if e = f.Sync(); e != nil {
		f.Close()
		return e
	}
	info, e := f.Stat()
	ce := f.Close()
	if e != nil {
		return e
	}
	if ce != nil {
		return ce
	}
	if e = ctx.Err(); e != nil {
		return e
	}
	sourceFiles := map[string]int64{"source/manifest.json": int64(len(v.Raw))}
	for _, a := range files {
		sourceFiles["source/"+a.Path] = a.Bytes
	}
	audio := []string{}
	for _, seg := range v.Manifest.Audio.Segments {
		audio = append(audio, "source/"+seg.Artifact.Path)
	}
	imu := ""
	if v.Manifest.IMU.Artifact.Path != "" {
		imu = "source/" + v.Manifest.IMU.Artifact.Path
	}
	b, e := json.MarshalIndent(map[string]any{"session_id": v.ID, "display_name": v.Name, "recording_time": sessionTime(v), "exported_at": time.Now().UTC().Format(time.RFC3339Nano), "directory_naming_version": 1, "source_manifest_sha256": manifestSHA, "file": "video.mp4", "bytes": info.Size(), "media_hash_verified": false, "format": "H.264 SBS MP4", "source_copies_retained": true, "source_files": sourceFiles, "imu_file": imu, "audio_files": audio, "recording_data_note": "source 保留录制目录内全部文件及原始目录结构；IMU/音频不转换、不插值；空路径/列表表示清单未声明该数据，不制造缺失数据。逐文件核对字节数，未校验媒体 SHA256。", "timeline_repaired": repaired, "timing_note": "合成视频仅供观看，不能用于与原始 IMU 精确对齐；同步分析请使用 source 原始视频、音频、IMU、帧时间戳及 manifest。保留捕获时间间隔；仅在受限预检通过后修正重复/轻微倒退时间戳；非 SDK 同步认证"}, "", "  ")
	if e != nil {
		return e
	}
	return writeNew(st, "export.json", b)
}

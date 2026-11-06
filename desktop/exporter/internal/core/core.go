package core

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"openaria-exporter/internal/source"
	"os"
	"path"
	"path/filepath"
	"sort"
	"strings"
)

type Artifact struct {
	Path  string `json:"path"`
	Bytes int64  `json:"bytes"`
	SHA   string `json:"sha256"`
}
type Segment struct {
	Index     int     `json:"index"`
	Start     float64 `json:"start_time_seconds"`
	End       float64 `json:"end_time_seconds"`
	Artifacts struct {
		Left  Artifact `json:"left"`
		Right Artifact `json:"right"`
	} `json:"artifacts"`
	Artifact Artifact `json:"artifact"`
}
type Manifest struct {
	Schema   string `json:"schema"`
	VolumeID string `json:"volume_id"`
	ID       string `json:"session_id"`
	Sealed   bool   `json:"sealed"`
	Name     string `json:"display_name"`
	Time     struct {
		Started  string  `json:"started_at"`
		Duration float64 `json:"duration_seconds"`
	} `json:"time"`
	Video struct {
		Segments []Segment `json:"segments"`
	} `json:"video"`
	Audio struct {
		Segments []Segment `json:"segments"`
	} `json:"audio"`
}
type Session struct {
	Dir, ID, Name, Status string
	Manifest              Manifest
	Artifacts             []Artifact
	Raw                   []byte
	Error                 string
	Bytes                 int64
	MetadataSource        string
	MetadataWarning       string
	TakeID                string
	TakeSequence          int
	LocalExportedAt       string `json:"local_exported_at,omitempty"`
	LocalExportPath       string `json:"local_export_path,omitempty"`
}

func Scan(ctx context.Context, s source.Source) ([]Session, error) {
	var out []Session
	var walk func(string, int) error
	walk = func(p string, depth int) error {
		if e := ctx.Err(); e != nil {
			return e
		}
		if depth > 5 {
			return nil
		}
		ds, e := s.List(p)
		if e != nil {
			return e
		}
		sort.Slice(ds, func(i, j int) bool { return ds[i].Name < ds[j].Name })
		for _, d := range ds {
			if !d.Dir && d.Name == "manifest.json" {
				v := Session{Dir: p, MetadataSource: "manifest"}
				r, e := s.Open(path.Join(p, d.Name))
				if e != nil {
					return e
				}
				v.Raw, e = io.ReadAll(io.LimitReader(r, 8<<20+1))
				r.Close()
				if e != nil {
					return e
				}
				if len(v.Raw) > 8<<20 {
					v.Error = "manifest 超过 8MiB"
				} else {
					v.Error = parse(&v)
				}
				v.ID = v.Manifest.ID
				v.Name = v.Manifest.Name
				if v.ID == "" {
					v.ID = path.Base(p)
				}
				if strings.Contains(p, ".partial") || !v.Manifest.Sealed {
					v.Status = "未封存 / partial"
				}
				if v.Error != "" {
					v.Status = "manifest 错误: " + v.Error
				}
				files, err := inventory(ctx, s, p)
				if err != nil {
					return err
				}
				for _, f := range files {
					v.Bytes += f.Bytes
				}
				out = append(out, v)
				return nil
			}
		}
		for _, d := range ds {
			if d.Dir {
				if e := walk(path.Join(p, d.Name), depth+1); e != nil {
					return e
				}
			}
		}
		return nil
	}
	e := walk(".", 0)
	if e == nil {
		e = enrichCatalog(ctx, s, out)
	}
	return out, e
}
func parse(v *Session) string {
	if e := json.Unmarshal(v.Raw, &v.Manifest); e != nil {
		return e.Error()
	}
	m := v.Manifest
	if !strings.HasPrefix(m.Schema, "ylx.device-session.v") || !(m.Schema == "ylx.device-session.v1" || m.Schema == "ylx.device-session.v2" || m.Schema == "ylx.device-session.v3" || m.Schema == "ylx.device-session.v4") {
		return "未知 schema"
	}
	if source.Safe(m.ID) != nil || strings.Contains(m.ID, "/") || m.ID == "." {
		return "无效 session_id"
	}
	var root any
	if e := json.Unmarshal(v.Raw, &root); e != nil {
		return e.Error()
	}
	seen := map[string]bool{}
	var err string
	var visit func(any)
	visit = func(x any) {
		switch a := x.(type) {
		case map[string]any:
			if p, ok := a["path"].(string); ok {
				b, bok := a["bytes"].(float64)
				h, hok := a["sha256"].(string)
				h = strings.TrimPrefix(h, "sha256:")
				raw, e := hex.DecodeString(h)
				if source.Safe(p) != nil || p == "." || !bok || b < 0 || b != float64(int64(b)) || !hok || e != nil || len(raw) != 32 {
					err = "无效 artifact"
					return
				}
				if seen[p] {
					err = "重复 artifact 路径"
					return
				}
				seen[p] = true
				v.Artifacts = append(v.Artifacts, Artifact{p, int64(b), h})
				return
			}
			for _, val := range a {
				visit(val)
			}
		case []any:
			for _, val := range a {
				visit(val)
			}
		}
	}
	visit(root)
	if err != "" {
		return err
	}
	if len(v.Artifacts) == 0 || len(m.Video.Segments) == 0 {
		return "缺少 artifacts / video segments"
	}
	for _, seg := range m.Video.Segments {
		if seg.Index < 0 || seg.End <= seg.Start || !seen[seg.Artifacts.Left.Path] || !seen[seg.Artifacts.Right.Path] {
			return "无效视频段"
		}
	}
	return ""
}

type Progress struct {
	Bytes int64
	File  string
	Rate  float64
	Stage string
}
type Options struct {
	Output             string
	Playback, Hardware bool
	Serial             bool
	VideoOnly          bool
	RawFallback        bool // Preserve unprocessed media if video processing fails.
	rawOnly            bool
	stream             *mediaStream
	batchTestWorkers   int
	transferTest       *transferTestHooks
	Progress           func(Progress)
}
type Receipt struct {
	Session      string
	ManifestSHA  string
	Mode         string
	Integrity    string
	Completeness string
	Timing       string
	Files        map[string]string
	Provenance   map[string]string
}

func exportOnce(ctx context.Context, s source.Source, v Session, o Options) (result string, exportErr error) {
	if o.VideoOnly {
		o.Playback = true
		for _, tool := range []string{"ffmpeg", "ffprobe"} {
			if _, err := mediaTool(tool); err != nil {
				return "", &processingError{err}
			}
		}
	}
	timings := newSessionTimings(v.ID, o.Serial)
	defer timings.log()
	if e := source.ValidateOutput(s, o.Output); e != nil {
		return "", e
	}
	if v.Error != "" {
		return "", fmt.Errorf("%s", v.Error)
	}
	mode := "archive"
	if o.Playback {
		mode = "playback"
	}
	if o.VideoOnly {
		mode = "video"
	}
	if o.rawOnly {
		mode = "raw"
	}
	key := sha256.Sum256(v.Raw)
	receipt := Receipt{v.ID, hex.EncodeToString(key[:]), mode, "逐文件 SHA256 已验证", "未执行完整 schema/录制完整性认证", "未验证同步", map[string]string{}, map[string]string{}}
	if strings.Contains(v.Status, "partial") {
		receipt.Completeness = "partial / 未封存"
	}
	if e := os.MkdirAll(o.Output, 0755); e != nil {
		return "", e
	}
	root, e := os.OpenRoot(o.Output)
	if e != nil {
		return "", e
	}
	defer root.Close()
	name := v.ID + "-" + mode
	stage := "." + name + ".partial"
	// Keep a stable lock outside the directory being renamed. Windows refuses
	// to rename a directory containing an open file, even with delete sharing.
	// Leave this lock file in place: unlinking it would let waiters lock different
	// file objects for the same export. The OS releases the lock on close/crash.
	publishLockName := "." + name + ".lock"
	if e = targetSafe(root, publishLockName); e != nil {
		return "", e
	}
	publishLock, e := root.OpenFile(publishLockName, os.O_CREATE|os.O_RDWR, 0600)
	if e != nil {
		return "", e
	}
	defer publishLock.Close()
	if e = lockFile(publishLock); e != nil {
		return "", fmt.Errorf("另一个导出任务正在使用此目录: %w", e)
	}
	if _, e = root.Lstat(name); !os.IsNotExist(e) {
		return "", fmt.Errorf("目标冲突（不覆盖）: %s", name)
	}
	if e = root.Mkdir(stage, 0700); e != nil && !os.IsExist(e) {
		return "", e
	}
	info, e := root.Lstat(stage)
	if e != nil || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
		return "", fmt.Errorf("无效 staging")
	}
	st, e := root.OpenRoot(stage)
	if e != nil {
		return "", e
	}
	defer st.Close()
	claim := receipt.ManifestSHA + "\n" + mode
	if e = targetSafe(st, ".claim"); e != nil {
		return "", e
	}
	old, e := st.ReadFile(".claim")
	if e == nil {
		if string(old) != claim {
			return "", fmt.Errorf("staging 所有权冲突")
		}
	} else if os.IsNotExist(e) {
		ds, e := st.Open(".")
		if e != nil {
			return "", e
		}
		entries, e := ds.ReadDir(-1)
		ds.Close()
		if e != nil || len(entries) > 0 {
			return "", fmt.Errorf("未知 staging 内容")
		}
		f, e := st.OpenFile(".claim", os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
		if e != nil {
			return "", e
		}
		_, e = f.WriteString(claim)
		f.Close()
		if e != nil {
			return "", e
		}
	} else {
		return "", e
	}
	for _, p := range []string{".lock", "source", "work", "receipt.json", "playback.mp4", "video.mp4", "export.json"} {
		if e = targetSafe(st, p); e != nil {
			return "", e
		}
	}
	lock, e := st.OpenFile(".lock", os.O_CREATE|os.O_RDWR, 0600)
	if e != nil {
		return "", e
	}
	defer lock.Close()
	if e = lockFile(lock); e != nil {
		return "", fmt.Errorf("另一个导出任务正在使用此目录: %w", e)
	}
	if e = st.MkdirAll("source", 0700); e != nil {
		return "", e
	}
	if e = st.MkdirAll("work", 0700); e != nil {
		return "", e
	}
	data, e := st.OpenRoot("source")
	if e != nil {
		return "", e
	}
	defer data.Close()
	if o.VideoOnly {
		for _, p := range []string{"export.json", "video.mp4", "playback.mp4"} {
			if e := st.Remove(p); e != nil && !os.IsNotExist(e) {
				return "", e
			}
		}
		defer func() {
			// Remove only our media staging while the session lock is still held,
			// including failures/cancellation; never remove anything on the source.
			data.Close()
			exportErr = errors.Join(exportErr, st.RemoveAll("source"), st.RemoveAll("work"))
		}()
	}
	// A batch stream may already be prefetching another session. Serialize
	// metadata traversal with its source reads (ext4 directory caches are mutable).
	if o.stream != nil {
		o.stream.mu.Lock()
	}
	files, e := inventory(ctx, s, v.Dir)
	if o.stream != nil {
		o.stream.mu.Unlock()
	}
	if e != nil {
		return "", e
	}
	expect := map[string]Artifact{}
	for _, a := range v.Artifacts {
		expect[a.Path] = a
	}
	if o.VideoOnly || o.rawOnly {
		expect = mediaArtifacts(v)
		selected := make([]Artifact, 0, len(expect))
		for _, a := range files {
			if _, ok := expect[a.Path]; ok {
				selected = append(selected, a)
			}
		}
		files = selected
	}
	found := map[string]bool{}
	var required uint64
	for i, a := range files {
		if expected, ok := expect[a.Path]; ok {
			if expected.Bytes != a.Bytes {
				return "", fmt.Errorf("源文件大小改变 %s", a.Path)
			}
			files[i].SHA = expected.SHA
			found[a.Path] = true
			receipt.Provenance[a.Path] = "manifest"
		} else {
			receipt.Provenance[a.Path] = "无 manifest 期望；源读取哈希"
		}
		if _, e := data.Lstat(a.Path); os.IsNotExist(e) {
			required += uint64(a.Bytes)
		}
	}
	for p := range expect {
		if !found[p] {
			return "", fmt.Errorf("manifest 文件缺失 %s", p)
		}
	}
	if o.VideoOnly {
		required = uint64(float64(mediaBitrate(v)+256000) / 8 * mediaDuration(v) * 1.1)
	} else if o.Playback {
		required += uint64(v.Bytes) * 2
	}
	free, e := available(o.Output)
	if e != nil {
		return "", e
	}
	if free < required+(16<<20) {
		return "", fmt.Errorf("目标空间不足：需要 %d，可用 %d", required+(16<<20), free)
	}
	if o.rawOnly {
		// Raw fallback is a new, independently locked output, never a video success.
		defer func() {
			data.Close()
			if exportErr != nil {
				exportErr = errors.Join(exportErr, st.RemoveAll("source"), st.RemoveAll("work"))
			}
		}()
		if e = copyRawMedia(ctx, s, v, o, data, files); e != nil {
			return "", e
		}
		receipt.Integrity = "逐文件复制并核对字节数；未校验媒体 SHA256"
		receipt.Timing = "原始分段，未拼接、未转码，音画同步未处理"
	} else if !o.VideoOnly {
		if e = transfer(ctx, s, v, o, st, data, files, &receipt, timings); e != nil {
			return "", e
		}
	}
	if !o.VideoOnly {
		if e = writeNew(data, "manifest.json", v.Raw); e != nil {
			return "", e
		}
	}
	if o.Playback {
		for _, p := range []string{"work/stream-0.txt", "work/stream-1.txt", "work/stream-2.txt", "work/playback.mp4"} {
			if e = targetSafe(st, p); e != nil {
				return "", e
			}
		}
	}
	if o.VideoOnly {
		g := o.stream
		if g == nil {
			g, e = newMediaStream(ctx, s, []Session{v})
			if e != nil {
				return "", e
			}
			defer g.Close()
		}
		if e = renderStreaming(ctx, filepath.Join(o.Output, stage), v, g, o.Progress, timings); e != nil {
			if ctx.Err() != nil {
				return "", ctx.Err()
			}
			if readErr := g.Err(); readErr != nil {
				return "", readErr
			}
			return "", &processingError{e}
		}
	} else if o.Playback {
		for _, seg := range v.Manifest.Video.Segments {
			for _, p := range []string{seg.Artifacts.Left.Path, seg.Artifacts.Right.Path} {
				if _, ok := receipt.Files[p]; !ok {
					return "", fmt.Errorf("未校验媒体路径 %s", p)
				}
			}
		}
		for _, seg := range v.Manifest.Audio.Segments {
			if _, ok := receipt.Files[seg.Artifact.Path]; !ok {
				return "", fmt.Errorf("未校验音频路径")
			}
		}
		if e = render(ctx, filepath.Join(o.Output, stage), v, o.Hardware, o.Progress, timings); e != nil {
			return "", e
		}
		receipt.Timing = "按 manifest index 依次拼接，段内解码时间戳；不等同 SDK 单调时钟时间线重建；音画同步未认证"
	}
	if o.VideoOnly {
		if e = finishVideo(ctx, st, v, receipt.ManifestSHA); e != nil {
			return "", e
		}
		data.Close()
		if e = st.RemoveAll("source"); e != nil {
			return "", e
		}
		if e = st.RemoveAll("work"); e != nil {
			return "", e
		}
	} else {
		b, _ := json.MarshalIndent(receipt, "", "  ")
		if e = writeNew(st, "receipt.json", b); e != nil {
			return "", e
		}
		if o.rawOnly {
			const note = "视频处理未完成。source 文件夹保存的是卡内原始左右眼视频、音频分段和 manifest.json，未拼接、未转码，也未重建音画同步。\n这不是合并成品；应用未将其标记为合并导出成功。卡内数据未修改。\n"
			if e = targetSafe(st, "UNPROCESSED.txt"); e != nil {
				return "", e
			}
			if e = writeNew(st, "UNPROCESSED.txt", []byte(note)); e != nil {
				return "", e
			}
			if e = st.RemoveAll("work"); e != nil {
				return "", e
			}
		}
	}
	if e = ctx.Err(); e != nil {
		return "", e
	}
	// Windows also refuses directory publication while a child directory handle
	// is open. Video mode has already closed data; archive mode closes it here.
	data.Close()
	// The parent lock still excludes other exporters while this compatibility
	// staging lock is closed and the completed directory is published.
	if e = lock.Close(); e != nil {
		return "", e
	}
	if e = publish(root, stage, name); e != nil {
		return "", fmt.Errorf("发布导出目录: %w", e)
	}
	return filepath.Join(o.Output, name), nil
}
func writeNew(r *os.Root, p string, b []byte) error {
	old, e := r.ReadFile(p)
	if e == nil {
		if string(old) == string(b) {
			return nil
		}
		return fmt.Errorf("文件冲突 %s", p)
	}
	f, e := r.OpenFile(p, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
	if e != nil {
		return e
	}
	_, e = f.Write(b)
	if e == nil {
		e = f.Sync()
	}
	ce := f.Close()
	if e == nil {
		e = ce
	}
	return e
}
func copyContext(ctx context.Context, w io.Writer, r io.Reader, b []byte, progress func(int)) (int64, error) {
	var total int64
	for {
		if e := ctx.Err(); e != nil {
			return total, e
		}
		n, e := r.Read(b)
		if n > 0 {
			nn, we := w.Write(b[:n])
			total += int64(nn)
			if we != nil {
				return total, we
			}
			if nn != n {
				return total, io.ErrShortWrite
			}
			if progress != nil {
				progress(n)
			}
		}
		if e == io.EOF {
			return total, nil
		}
		if e != nil {
			return total, e
		}
	}
}

func targetSafe(r *os.Root, p string) error {
	if e := source.Safe(p); e != nil {
		return e
	}
	cur := ""
	for _, part := range strings.Split(p, "/") {
		cur = path.Join(cur, part)
		s, e := r.Lstat(cur)
		if os.IsNotExist(e) {
			continue
		}
		if e != nil {
			return e
		}
		if s.Mode()&os.ModeSymlink != 0 {
			return fmt.Errorf("目标含符号链接: %s", cur)
		}
	}
	return nil
}

func inventory(ctx context.Context, s source.Source, base string) ([]Artifact, error) {
	var out []Artifact
	seen := map[string]bool{}
	var walk func(string, int) error
	walk = func(p string, depth int) error {
		if e := ctx.Err(); e != nil {
			return e
		}
		if depth > 64 || len(out) > 100000 {
			return fmt.Errorf("录像目录超出安全限制")
		}
		ds, e := s.List(path.Join(base, p))
		if e != nil {
			return e
		}
		sort.Slice(ds, func(i, j int) bool { return ds[i].Name < ds[j].Name })
		for _, d := range ds {
			q := path.Join(p, d.Name)
			if e = source.Safe(q); e != nil {
				return e
			}
			key := strings.ToLower(q)
			if seen[key] {
				return fmt.Errorf("跨平台路径冲突 %s", q)
			}
			seen[key] = true
			if d.Dir {
				if e = walk(q, depth+1); e != nil {
					return e
				}
			} else {
				out = append(out, Artifact{Path: q, Bytes: d.Size})
			}
		}
		return nil
	}
	e := walk(".", 0)
	return out, e
}

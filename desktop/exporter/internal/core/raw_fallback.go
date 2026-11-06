package core

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log"
	"openaria-exporter/internal/source"
	"os"
	"path"
)

// RawFallbackError is a partial success: original media is safe locally, but no
// processed video exists. Callers must warn, and must not record a video mark.
type RawFallbackError struct {
	Path  string
	Cause error
}

func (e *RawFallbackError) Error() string {
	return fmt.Sprintf("原始文件已保存，但视频处理未完成（未拼接、未转码）。\n保存位置：%s\n卡内原始数据未修改；未标记为合并导出成功。\n处理失败原因：%v", e.Path, e.Cause)
}
func (e *RawFallbackError) Unwrap() error { return e.Cause }

type processingError struct{ error }

func Export(ctx context.Context, s source.Source, v Session, o Options) (string, error) {
	dst, err := exportOnce(ctx, s, v, o)
	var processing *processingError
	if !o.RawFallback || !o.VideoOnly || !errors.As(err, &processing) || ctx.Err() != nil {
		return dst, err
	}
	log.Printf("video processing failed; preserving raw media session=%s reason=%v", v.ID, err)
	raw := o
	raw.VideoOnly, raw.Playback, raw.RawFallback, raw.rawOnly = false, false, false, true
	dst, copyErr := exportOnce(ctx, s, v, raw)
	if copyErr != nil {
		log.Printf("raw fallback failed session=%s reason=%v", v.ID, copyErr)
		return "", fmt.Errorf("视频处理失败：%v；原始文件兜底导出也未完成：%w", err, copyErr)
	}
	log.Printf("raw fallback published session=%s path=%q bytes=%d", v.ID, dst, MediaBytes(v))
	return dst, &RawFallbackError{Path: dst, Cause: err}
}

func copyRawMedia(ctx context.Context, s source.Source, v Session, o Options, data *os.Root, files []Artifact) error {
	stage := "保存完整录制数据"
	if o.rawOnly {
		stage = "处理失败，保存原始文件"
	}
	// Share the batch's physical-source lock, including Open/Read/Close, without
	// adding mutable entries to its HTTP media index or hashing large recordings.
	withSource := func(fn func() error) error {
		if o.stream != nil {
			o.stream.mu.Lock()
			defer o.stream.mu.Unlock()
		}
		return fn()
	}
	// Reject stale partials before writing anything. Never clean up a file from
	// a previous run merely because a new copy hit O_EXCL or was canceled.
	for _, a := range files {
		if _, err := data.Lstat(a.Path); err == nil {
			return fmt.Errorf("原始暂存文件已存在，保留不覆盖：%s", a.Path)
		} else if !os.IsNotExist(err) {
			return err
		}
	}
	var created []string
	complete := false
	defer func() {
		if !complete {
			for _, p := range created {
				_ = data.Remove(p)
			}
		}
	}()
	var total int64
	if o.VideoOnly {
		total = MediaBytes(v)
	}
	buf := make([]byte, 1<<20)
	for _, a := range files {
		if err := ctx.Err(); err != nil {
			return err
		}
		if err := targetSafe(data, a.Path); err != nil {
			return err
		}
		if err := data.MkdirAll(path.Dir(a.Path), 0700); err != nil {
			return err
		}
		f, err := data.OpenFile(a.Path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
		if err != nil {
			return err
		}
		created = append(created, a.Path)
		var src io.ReadCloser
		err = withSource(func() (e error) { src, e = s.Open(path.Join(v.Dir, a.Path)); return e })
		if err != nil {
			f.Close()
			return err
		}
		if o.Progress != nil {
			o.Progress(Progress{Bytes: total, File: a.Path, Stage: stage})
		}
		reader := sourceLockedReader{src, withSource}
		n, err := copyContext(ctx, f, io.LimitReader(reader, a.Bytes), buf, func(n int) {
			total += int64(n)
			if o.Progress != nil {
				o.Progress(Progress{Bytes: total, File: a.Path, Stage: stage})
			}
		})
		if err == nil && n != a.Bytes {
			err = io.ErrUnexpectedEOF
		}
		if err == nil {
			var extra [1]byte
			n, end := reader.Read(extra[:])
			if n != 0 || end != io.EOF {
				err = fmt.Errorf("源文件大小改变或读取未结束 %s: %v", a.Path, end)
			}
		}
		err = errors.Join(err, withSource(src.Close))
		if err == nil {
			err = f.Sync()
		}
		err = errors.Join(err, f.Close())
		if err != nil {
			return err
		}
	}
	// Refuse to publish a snapshot whose file membership changed during copying.
	var after []Artifact
	if err := withSource(func() (e error) { after, e = inventory(ctx, s, v.Dir); return e }); err != nil {
		return err
	}
	if len(after) != len(files) {
		return fmt.Errorf("复制期间录制目录内容改变")
	}
	for i, a := range after {
		if a.Path != files[i].Path || a.Bytes != files[i].Bytes {
			return fmt.Errorf("复制期间录制目录内容改变：%s", a.Path)
		}
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	complete = true
	return nil
}

type sourceLockedReader struct {
	r          io.Reader
	withSource func(func() error) error
}

func (r sourceLockedReader) Read(p []byte) (n int, err error) {
	err = r.withSource(func() error { n, err = r.r.Read(p); return err })
	return
}

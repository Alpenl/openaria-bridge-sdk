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
		return "", fmt.Errorf("视频处理失败：%v；原始文件兜底导出也未完成：%w", err, copyErr)
	}
	return dst, &RawFallbackError{Path: dst, Cause: err}
}

func copyRawMedia(ctx context.Context, s source.Source, v Session, o Options, data *os.Root, files []Artifact) error {
	g := o.stream
	if g == nil {
		var err error
		g, err = newMediaStream(ctx, s, []Session{v})
		if err != nil {
			return err
		}
		defer g.Close()
	}
	var total int64
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
		id, ok := g.index[path.Join(v.Dir, a.Path)]
		if !ok {
			f.Close()
			return fmt.Errorf("媒体未列入只读清单：%s", a.Path)
		}
		if o.Progress != nil {
			o.Progress(Progress{Bytes: total, File: a.Path, Stage: "处理失败，保存原始文件"})
		}
		n, err := copyContext(ctx, f, io.NewSectionReader(streamReader{g, id}, 0, a.Bytes), buf, func(n int) {
			total += int64(n)
			if o.Progress != nil {
				o.Progress(Progress{Bytes: total, File: a.Path, Stage: "处理失败，保存原始文件"})
			}
		})
		if err == nil && n != a.Bytes {
			err = io.ErrUnexpectedEOF
		}
		if err == nil {
			err = f.Sync()
		}
		err = errors.Join(err, f.Close())
		if err != nil {
			return err
		}
	}
	return g.Err()
}

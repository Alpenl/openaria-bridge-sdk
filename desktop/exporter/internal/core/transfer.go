package core

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"openaria-exporter/internal/source"
	"os"
	"path"
	"time"
)

const unverifiedBudget int64 = 128 << 20

// Private, per-export barriers allow deterministic scheduling tests without
// global hooks or a public tuning API. Production always uses the fixed budget.
type transferTestHooks struct {
	budget int64
	verify func(Artifact) error
	wait   func(Artifact)
}
type transferJob struct {
	artifact Artifact
	tmp      string
	file     *os.File
	done     chan error
}

func verifyTransfer(ctx context.Context, st *os.Root, j *transferJob, buf []byte, timing *sessionTimings, hooks *transferTestHooks) error {
	start := time.Now()
	err := j.file.Sync()
	closeErr := j.file.Close()
	timing.sync += time.Since(start)
	if err != nil {
		return err
	}
	if closeErr != nil {
		return closeErr
	}
	start = time.Now()
	defer func() { timing.verify += time.Since(start) }()
	if hooks != nil && hooks.verify != nil {
		if err := hooks.verify(j.artifact); err != nil {
			return err
		}
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	f, err := st.Open(j.tmp)
	if err != nil {
		return err
	}
	h := sha256.New()
	n, err := copyContext(ctx, h, f, buf, nil)
	ce := f.Close()
	if err != nil {
		return err
	}
	if ce != nil {
		return ce
	}
	if n != j.artifact.Bytes || hex.EncodeToString(h.Sum(nil)) != j.artifact.SHA {
		return fmt.Errorf("目标复核失败 %s", j.artifact.Path)
	}
	return nil
}

func transfer(parent context.Context, s source.Source, v Session, o Options, st, data *os.Root, files []Artifact, receipt *Receipt, timing *sessionTimings) (err error) {
	ctx, cancel := context.WithCancelCause(parent)
	jobs := make(chan *transferJob, 2)
	joined := make(chan struct{})
	var workerErr error
	go func() {
		defer close(joined)
		buf := make([]byte, 1<<20)
		for j := range jobs {
			e := verifyTransfer(ctx, st, j, buf, timing, o.transferTest)
			if e != nil {
				if workerErr == nil {
					workerErr = e
				}
				cancel(e)
			}
			j.done <- e
		}
	}()
	defer func() {
		cancel(err)
		close(jobs)
		<-joined
		// A target I/O failure that stopped a source copy must not become merely
		// context.Canceled. A real coordinator error still takes precedence.
		if err != nil && (errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded)) {
			if workerErr != nil && !errors.Is(workerErr, context.Canceled) && !errors.Is(workerErr, context.DeadlineExceeded) {
				err = workerErr
			} else if cause := context.Cause(ctx); cause != nil {
				err = cause
			}
		}
	}()
	var pending []*transferJob
	var reserved int64
	finish := func() error {
		j := pending[0]
		if o.transferTest != nil && o.transferTest.wait != nil {
			o.transferTest.wait(j.artifact)
		}
		e := <-j.done
		if e != nil {
			return e
		}
		if e := ctx.Err(); e != nil {
			return e
		}
		if e := st.Rename(j.tmp, "source/"+j.artifact.Path); e != nil {
			return e
		}
		receipt.Files[j.artifact.Path] = j.artifact.SHA
		reserved -= j.artifact.Bytes
		pending = pending[1:]
		return nil
	}
	drain := func() error {
		for len(pending) > 0 {
			if e := finish(); e != nil {
				return e
			}
		}
		return nil
	}
	budget := unverifiedBudget
	if o.transferTest != nil && o.transferTest.budget > 0 {
		budget = o.transferTest.budget
	}
	var total int64
	start, last := time.Now(), time.Now()
	buf := make([]byte, 1<<20)
	slot := 0
	for _, a := range files {
		if e := ctx.Err(); e != nil {
			return e
		}
		if e := targetSafe(data, a.Path); e != nil {
			return e
		}
		// Recovery is serialized, including hashing sidecars from the source.
		if _, e := data.Lstat(a.Path); e == nil {
			if e := drain(); e != nil {
				return e
			}
			if o.Progress != nil {
				o.Progress(Progress{Bytes: total, File: a.Path, Stage: "复核恢复文件"})
			}
			f, e := data.Open(a.Path)
			if e != nil {
				return e
			}
			h := sha256.New()
			began := time.Now()
			n, e := copyContext(ctx, h, f, buf, nil)
			ce := f.Close()
			timing.verify += time.Since(began)
			if e != nil {
				return e
			}
			if ce != nil {
				return ce
			}
			if a.SHA == "" {
				r, e := s.Open(path.Join(v.Dir, a.Path))
				if e != nil {
					return e
				}
				sh := sha256.New()
				began = time.Now()
				sn, e := copyContext(ctx, sh, r, buf, nil)
				ce := r.Close()
				timing.copy += time.Since(began)
				if e != nil {
					return e
				}
				if ce != nil {
					return ce
				}
				if sn != a.Bytes {
					return fmt.Errorf("源文件大小改变 %s", a.Path)
				}
				a.SHA = hex.EncodeToString(sh.Sum(nil))
			}
			if n != a.Bytes || hex.EncodeToString(h.Sum(nil)) != a.SHA {
				return fmt.Errorf("恢复文件校验失败 %s", a.Path)
			}
			receipt.Files[a.Path] = a.SHA
			total += a.Bytes
			continue
		} else if !os.IsNotExist(e) {
			return e
		}
		// Reserve before opening/copying. Oversize files drain and run alone.
		for len(pending) > 0 && (o.Serial || len(pending) == 2 || a.Bytes > budget || reserved > budget-a.Bytes) {
			if e := finish(); e != nil {
				return e
			}
		}
		if e := ctx.Err(); e != nil {
			return e
		}
		if e := data.MkdirAll(path.Dir(a.Path), 0700); e != nil {
			return e
		}
		tmp := fmt.Sprintf("work/transfer-%d", slot)
		slot = 1 - slot
		if info, e := st.Lstat(tmp); e == nil && (!info.Mode().IsRegular() || info.Mode()&os.ModeSymlink != 0) {
			return fmt.Errorf("不安全临时文件")
		} else if e != nil && !os.IsNotExist(e) {
			return e
		}
		f, e := st.OpenFile(tmp, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0600)
		if e != nil {
			return e
		}
		began := time.Now()
		r, e := s.Open(path.Join(v.Dir, a.Path))
		if e != nil {
			f.Close()
			return e
		}
		h := sha256.New()
		n, e := copyContext(ctx, io.MultiWriter(f, h), r, buf, func(n int) {
			total += int64(n)
			if o.Progress != nil && time.Since(last) > 150*time.Millisecond {
				last = time.Now()
				o.Progress(Progress{total, a.Path, float64(total) / time.Since(start).Seconds(), "复制"})
			}
		})
		ce := r.Close()
		timing.copy += time.Since(began)
		if e == nil {
			e = ce
		}
		if e != nil {
			f.Close()
			return fmt.Errorf("%s: %w", a.Path, e)
		}
		hash := hex.EncodeToString(h.Sum(nil))
		if a.SHA == "" {
			a.SHA = hash
		}
		if n != a.Bytes || hash != a.SHA {
			f.Close()
			return fmt.Errorf("源文件大小/哈希不符: %s", a.Path)
		}
		if o.Progress != nil {
			o.Progress(Progress{total, a.Path, 0, "复核目标"})
		}
		j := &transferJob{a, tmp, f, make(chan error, 1)}
		pending = append(pending, j)
		reserved += a.Bytes
		jobs <- j
		if o.Serial || a.Bytes > budget {
			if e := drain(); e != nil {
				return e
			}
		}
	}
	return drain()
}

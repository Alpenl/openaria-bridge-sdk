package source

import (
	"fmt"
	"io"
)

// AlignedReader adapts bounded unaligned filesystem requests to physical disks.
// Each physical read is at most 1 MiB plus two sectors.
type AlignedReader struct {
	Reader io.ReaderAt
	Sector int64
}

func (a AlignedReader) ReadAt(p []byte, off int64) (int, error) {
	if a.Sector <= 0 || a.Sector > 65536 || off < 0 {
		return 0, fmt.Errorf("invalid disk alignment")
	}
	done := 0
	for done < len(p) {
		n := len(p) - done
		if n > 1<<20 {
			n = 1 << 20
		}
		pos := off + int64(done)
		start := pos / a.Sector * a.Sector
		delta := int(pos - start)
		size := (int64(delta+n) + a.Sector - 1) / a.Sector * a.Sector
		b := make([]byte, size)
		nr, e := a.Reader.ReadAt(b, start)
		available := nr - delta
		if available < 0 {
			available = 0
		}
		if available > n {
			available = n
		}
		copy(p[done:], b[delta:delta+available])
		done += available
		if available < n {
			if e == nil {
				e = io.ErrUnexpectedEOF
			}
			return done, e
		}
	}
	return done, nil
}

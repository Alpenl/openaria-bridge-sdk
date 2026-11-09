package core

import (
	"context"
	"errors"
	"io"
	"net"
	"runtime"
	"syscall"
	"time"
)

// Retry ENOBUFS below net/http: ResponseWriter retains its first write error,
// so retrying at the HTTP layer cannot recover a transient socket allocation.
// This listener serves only the tokenized loopback media endpoint.
type mediaListener struct {
	net.Listener
	ctx context.Context
}

func (l mediaListener) Accept() (net.Conn, error) {
	c, err := l.Listener.Accept()
	if err != nil {
		return nil, err
	}
	return &mediaConn{Conn: c, ctx: l.ctx}, nil
}

type mediaConn struct {
	net.Conn
	ctx context.Context
}

func socketBufferFailure(err error) bool {
	// Winsock reports WSAENOBUFS (10055), not the synthetic Unix ENOBUFS.
	return errors.Is(err, syscall.ENOBUFS) || runtime.GOOS == "windows" && errors.Is(err, syscall.Errno(10055))
}

func (c *mediaConn) Write(p []byte) (int, error) {
	written := 0
	for retry := 0; ; retry++ {
		if err := c.ctx.Err(); err != nil {
			return written, err
		}
		n, err := c.Conn.Write(p[written:])
		written += n
		if err == nil {
			if written != len(p) {
				return written, io.ErrShortWrite
			}
			return written, nil
		}
		if !socketBufferFailure(err) || retry == 5 {
			return written, err
		}
		if written == len(p) {
			return written, nil
		}
		timer := time.NewTimer(time.Duration(1<<retry) * 10 * time.Millisecond)
		select {
		case <-timer.C:
		case <-c.ctx.Done():
			timer.Stop()
			return written, c.ctx.Err()
		}
	}
}

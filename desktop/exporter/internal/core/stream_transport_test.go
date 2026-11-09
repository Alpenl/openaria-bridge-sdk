package core

import (
	"bytes"
	"context"
	"errors"
	"net"
	"syscall"
	"testing"
)

type bufferFailureConn struct {
	net.Conn
	calls    int
	received bytes.Buffer
	fail     bool
	fatal    bool
	cancel   context.CancelFunc
}

func (c *bufferFailureConn) Write(p []byte) (int, error) {
	c.calls++
	if c.cancel != nil {
		c.cancel()
	}
	if c.fatal {
		return 0, net.ErrClosed
	}
	if c.calls == 1 || c.fail {
		n := min(3, len(p))
		c.received.Write(p[:n])
		return n, &net.OpError{Op: "write", Err: syscall.ENOBUFS}
	}
	return c.received.Write(p)
}

func TestStreamSocketBufferRecovery(t *testing.T) {
	for _, mode := range []string{"transient", "persistent", "closed", "canceled"} {
		t.Run(mode, func(t *testing.T) {
			ctx, cancel := context.WithCancel(t.Context())
			defer cancel()
			conn := &bufferFailureConn{fail: mode == "persistent", fatal: mode == "closed"}
			if mode == "canceled" {
				conn.cancel = cancel
			}
			c := &mediaConn{Conn: conn, ctx: ctx}
			payload := bytes.Repeat([]byte("0123456789"), 10)
			n, err := c.Write(payload)
			if mode == "transient" {
				if err != nil || n != len(payload) || !bytes.Equal(conn.received.Bytes(), payload) || conn.calls != 2 {
					t.Fatalf("duplicated/lost bytes n=%d err=%v calls=%d", n, err, conn.calls)
				}
			} else {
				if err == nil || conn.calls > 6 {
					t.Fatalf("unbounded/false success n=%d err=%v calls=%d", n, err, conn.calls)
				}
				if mode == "persistent" && (conn.calls != 6 || n != 18 || !socketBufferFailure(err)) {
					t.Fatal(n, err, conn.calls)
				}
				if mode == "closed" && (conn.calls != 1 || !errors.Is(err, net.ErrClosed)) {
					t.Fatal(err)
				}
				if mode == "canceled" && (conn.calls != 1 || !errors.Is(err, context.Canceled)) {
					t.Fatal(err)
				}
			}
		})
	}
}

package main

import (
	"context"
	"errors"
	"io"
	"path/filepath"
	"strings"
	"testing"
)

type probeReader struct {
	n                 int
	readErr, closeErr error
	closed            int
}

func (f *probeReader) ReadAt(b []byte, off int64) (int, error) {
	if len(b) != 512 || off != 0 {
		panic("unexpected probe")
	}
	return f.n, f.readErr
}
func (f *probeReader) Close() error { f.closed++; return f.closeErr }
func TestProbeAlwaysCloses(t *testing.T) {
	boom := errors.New("read failed")
	closeErr := errors.New("close failed")
	for _, tt := range []struct {
		name              string
		n                 int
		read, close, want error
	}{
		{"success", 512, nil, nil, nil}, {"short", 2, nil, nil, io.ErrUnexpectedEOF}, {"read", 0, boom, nil, boom}, {"close", 512, nil, closeErr, closeErr}, {"both", 0, boom, closeErr, boom},
	} {
		t.Run(tt.name, func(t *testing.T) {
			f := &probeReader{n: tt.n, readErr: tt.read, closeErr: tt.close}
			err := probeDevice(f)
			if !errors.Is(err, tt.want) || f.closed != 1 {
				t.Fatalf("err=%v closed=%d", err, f.closed)
			}
			if tt.close != nil && !errors.Is(err, tt.close) {
				t.Fatal(err)
			}
		})
	}
}
func TestAccessStatesAndFreshOpen(t *testing.T) {
	boom := errors.New("request=abc: operation not permitted")
	for _, tt := range []struct {
		name                        string
		statusErr, listErr, openErr error
		ds                          []Device
		want                        string
		lists, opens                int
	}{
		{name: "service unavailable", statusErr: boom, want: "服务连接失败"},
		{name: "waiting", want: "等待连接", lists: 1},
		{name: "enumeration", listErr: boom, want: "设备枚举失败", lists: 1},
		{name: "denied", ds: []Device{{Path: "one"}}, openErr: boom, want: "当前设备访问失败", lists: 1, opens: 1},
		{name: "readable", ds: []Device{{Path: "one"}, {Path: "selected"}}, want: "当前设备可读：selected", lists: 1, opens: 1},
	} {
		t.Run(tt.name, func(t *testing.T) {
			statuses, lists, opens := 0, 0, 0
			var handles []*probeReader
			check := func() accessResult {
				return checkDeviceAccess(context.Background(), "selected", func(context.Context) error { statuses++; return tt.statusErr }, func() ([]Device, error) {
					lists++
					if statuses != lists {
						t.Fatal("status must precede list")
					}
					return tt.ds, tt.listErr
				}, func(_ context.Context, p string) (accessReader, error) {
					opens++
					if tt.openErr != nil {
						return nil, tt.openErr
					}
					if p != "selected" {
						t.Fatal(p)
					}
					f := &probeReader{n: 512}
					handles = append(handles, f)
					return f, nil
				})
			}
			for i := 0; i < 2; i++ {
				r := check()
				if !strings.Contains(r.message, tt.want) {
					t.Fatal(r)
				}
				wantErr := tt.statusErr != nil || tt.listErr != nil || tt.openErr != nil
				if (r.err != nil) != wantErr {
					t.Fatal(r)
				}
			}
			if statuses != 2 || lists != tt.lists*2 || opens != tt.opens*2 {
				t.Fatalf("%d %d %d", statuses, lists, opens)
			}
			for _, f := range handles {
				if f.closed != 1 {
					t.Fatal("handle not closed")
				}
			}
		})
	}
}
func TestExactServiceEPERM(t *testing.T) {
	for _, s := range []string{"operation not permitted", "request=a: permission denied", "request=a: diskutil: operation not permitted", "prefix request=a: operation not permitted", "request=a: operation not permitted\n", "request=: operation not permitted", "request=a: operation not permitted extra"} {
		if devicePrivacyHint(errors.New(s)) != "" {
			t.Fatal(s)
		}
	}
	if devicePrivacyHint(nil) != "" || devicePrivacyHint(errors.New("request=abc-123: operation not permitted")) == "" {
		t.Fatal("classification")
	}
	err := authorizationError(context.Background(), errors.New("request=abc: operation not permitted"))
	if !strings.Contains(err.Error(), "设备访问配置") || !strings.Contains(err.Error(), "request=abc: operation not permitted") {
		t.Fatal(err)
	}
}
func TestShownIsNotAuthorization(t *testing.T) {
	marker := filepath.Join(t.TempDir(), "config", "shown")
	if !guideNeeded(marker) {
		t.Fatal("first launch")
	}
	if err := markGuideShown(marker); err != nil {
		t.Fatal(err)
	}
	if guideNeeded(marker) {
		t.Fatal("repeated guide")
	}
	denied := errors.New("request=a: operation not permitted")
	r := checkDeviceAccess(context.Background(), "", func(context.Context) error { return nil }, func() ([]Device, error) { return []Device{{Path: "device"}}, nil }, func(context.Context, string) (accessReader, error) { return nil, denied })
	if !errors.Is(r.err, denied) {
		t.Fatal("shown must not imply authorization")
	}
}

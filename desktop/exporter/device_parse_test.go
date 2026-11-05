package main

import (
	"context"
	"errors"
	"net"
	"openaria-exporter/internal/core"
	"strings"
	"testing"
)

func TestWindowsPartitionJSON(t *testing.T) {
	for _, tc := range []struct {
		s string
		n int
	}{{"", 0}, {"null", 0}, {"[]", 0}, {`{"Disk":1,"Partition":2,"Offset":4096,"Size":8192}`, 1}, {`[{"Disk":1},{"Disk":2}]`, 2}} {
		p, e := parseWinPartitions([]byte(tc.s))
		if e != nil || len(p) != tc.n {
			t.Fatalf("%s: %v %v", tc.s, p, e)
		}
	}
	if _, e := parseWinPartitions([]byte(`broken`)); e == nil {
		t.Fatal("invalid JSON")
	}
}
func TestAuthorizationMessages(t *testing.T) {
	for _, e := range []error{&net.DNSError{Err: "timeout /tmp/private.sock", IsTimeout: true}, context.Canceled, errors.New("osascript -128 /tmp/private.sock")} {
		s := authorizationError(context.Background(), e).Error()
		if !strings.Contains(s, "未保存详细日志") {
			t.Fatal(s)
		}
	}
}
func TestRenderProgressText(t *testing.T) {
	s := progressText(core.Progress{Stage: "合并编码", File: "00:00:01"}, 2, 3, 10, 30)
	if strings.Contains(s, "MiB/s") || !strings.Contains(s, "2/3") {
		t.Fatal(s)
	}
}

package source

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestIdentifyCard(t *testing.T) {
	good := map[string]string{"schema": "ylx.capture-volume.v1", "volume_id": "12345678-1234-4123-8123-123456789abc", "initialized_at": "2026-11-04T12:00:00.000Z"}
	for _, tc := range []struct {
		name, field, value, dir string
		missing, oversize       bool
		valid                   bool
	}{
		{name: "empty initialized", dir: "recordings", valid: true},
		{name: "legacy directory", dir: "sessions", valid: true},
		{name: "missing marker", dir: "recordings", missing: true},
		{name: "wrong schema", field: "schema", value: "other.v1", dir: "recordings"},
		{name: "bad uuid", field: "volume_id", value: "qa-volume", dir: "recordings"},
		{name: "bad time", field: "initialized_at", value: "yesterday", dir: "recordings"},
		{name: "extra field", field: "extra", value: "ignored", dir: "recordings"},
		{name: "no recordings"},
		{name: "oversized", dir: "recordings", oversize: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			root := t.TempDir()
			m := map[string]string{}
			for k, v := range good {
				m[k] = v
			}
			if tc.field != "" {
				m[tc.field] = tc.value
			}
			b, _ := json.Marshal(m)
			if tc.oversize {
				b = append(b, []byte(strings.Repeat(" ", 4097))...)
			}
			if !tc.missing {
				if e := os.WriteFile(filepath.Join(root, ".ylx-volume.json"), b, 0600); e != nil {
					t.Fatal(e)
				}
			}
			if tc.dir != "" {
				if e := os.Mkdir(filepath.Join(root, tc.dir), 0700); e != nil {
					t.Fatal(e)
				}
			}
			s, e := NewFolder(root)
			if e != nil {
				t.Fatal(e)
			}
			defer s.Close()
			id, e := IdentifyCard(s)
			if tc.valid {
				if e != nil || id.VolumeID != good["volume_id"] {
					t.Fatalf("%+v %v", id, e)
				}
			} else if !errors.Is(e, ErrNotCard) {
				t.Fatalf("expected not-card, got %v", e)
			}
		})
	}
}

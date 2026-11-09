package core

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"math"
	"openaria-exporter/internal/source"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestRecordingTimeNamesAndZones(t *testing.T) {
	for _, tc := range []struct {
		start, end   string
		duration     float64
		want, status string
	}{
		{"2026-11-09T17:30:28.426651+08:00", "", 1, "2026-11-09 17时30分28秒", "recorded"},
		{"2026-11-09T00:30:28-05:00", "", 1, "2026-11-09 00时30分28秒", "recorded"},
		{"2026-11-09T00:00:00Z", "", 1, "2026-11-09 00时00分00秒", "recorded"},
		{"2000-01-01T00:00:00Z", "2026-11-09T00:00:02+08:00", 4, "2026-11-08 23时59分58秒", "estimated"},
		{"2000-01-01T00:00:00Z", "", 1, "录制时间未知（1）", "unknown"},
		{"2026-11-09 12:00:00", "", 1, "录制时间未知（1）", "unknown"},
		{"2026-11-09T12:00:00+24:00", "", 1, "录制时间未知（1）", "unknown"},
		{"garbage", "2026-11-09T00:00:02Z", math.Inf(1), "录制时间未知（1）", "unknown"},
	} {
		t.Run(tc.start, func(t *testing.T) {
			v := Session{}
			v.Manifest.Time.Started = tc.start
			v.Manifest.Time.Ended = tc.end
			v.Manifest.Time.Duration = tc.duration
			if got := recordingName(v, "video", 1); got != tc.want {
				t.Fatalf("%s != %s", got, tc.want)
			}
			if got := sessionTime(v).Status; got != tc.status {
				t.Fatal(got)
			}
			if tc.status != "unknown" && recordingName(v, "video", 2) != tc.want+"（2）" {
				t.Fatal("collision name")
			}
			if !strings.HasSuffix(recordingName(v, "raw", 1), "（未合成）") {
				t.Fatal("missing raw label")
			}
		})
	}
}
func TestClockCalibrationProvenance(t *testing.T) {
	for _, kind := range []string{"client", "ntp", "hash", "session", "offset", "start", "unsynchronized", "end"} {
		t.Run(kind, func(t *testing.T) {
			v := Session{ID: "clock-session"}
			v.Dir = "."
			v.Manifest.Time.Started = "2026-11-09T17:30:28+08:00"
			v.Manifest.Time.Ended = "2026-11-09T17:30:29+08:00"
			v.Manifest.Time.Duration = 1
			start, _ := recordingDate(v.Manifest.Time.Started)
			c := map[string]any{"schema": "openaria.recording-clock.v1", "session_id": v.ID, "started_at": v.Manifest.Time.Started, "utc_offset_seconds": 28800, "calibration": map[string]any{"source": "client", "synchronized_at_ms": start.UnixMilli() - 10000}}
			switch kind {
			case "ntp", "unsynchronized":
				c["calibration"].(map[string]any)["source"] = kind
			case "session":
				c["session_id"] = "other"
			case "offset":
				c["utc_offset_seconds"] = 0
			case "start":
				c["started_at"] = "2026-11-09T17:40:28+08:00"
			case "end":
				v.Manifest.Time.Ended = "2026-11-10T17:30:29+08:00"
			}
			b, e := json.Marshal(c)
			if e != nil {
				t.Fatal(e)
			}
			h := sha256.Sum256(b)
			v.Artifacts = []Artifact{{Path: "clock-calibration.json", Bytes: int64(len(b)), SHA: hex.EncodeToString(h[:])}}
			if kind == "hash" {
				v.Artifacts[0].SHA = strings.Repeat("0", 64)
			}
			dir := t.TempDir()
			if e = os.WriteFile(filepath.Join(dir, "clock-calibration.json"), b, 0600); e != nil {
				t.Fatal(e)
			}
			s, e := source.NewFolder(dir)
			if e != nil {
				t.Fatal(e)
			}
			defer s.Close()
			loadRecordingTime(t.Context(), s, &v)
			if kind == "client" || kind == "ntp" {
				if v.RecordingTime.Status != "calibrated" || v.RecordingTime.Source != kind || recordingName(v, "video", 1) != "2026-11-09 17时30分28秒" {
					t.Fatalf("%+v", v.RecordingTime)
				}
			} else if v.RecordingTime.Status != "conflict" || recordingName(v, "video", 1) != "录制时间未知（1）" {
				t.Fatalf("accepted bad clock %+v", v.RecordingTime)
			}
		})
	}
}
func TestRecordingSortUsesInstants(t *testing.T) {
	vs := []Session{{ID: "unknown"}, {ID: "early"}, {ID: "later"}}
	vs[1].Manifest.Time.Started = "2026-11-09T08:00:00+08:00"
	vs[2].Manifest.Time.Started = "2026-11-09T01:00:00Z"
	sortRecordings(vs)
	if vs[0].ID != "early" || vs[1].ID != "later" || vs[2].ID != "unknown" {
		t.Fatal(vs)
	}
}

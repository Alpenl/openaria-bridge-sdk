package core

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"openaria-exporter/internal/source"
	"path"
	"sort"
	"time"
)

// RecordingTime is derived metadata; sealed manifests and sensor clocks stay intact.
type RecordingTime struct {
	StartedAt         string `json:"started_at,omitempty"`
	OriginalStartedAt string `json:"original_started_at,omitempty"`
	EndedAt           string `json:"ended_at,omitempty"`
	Timezone          string `json:"timezone,omitempty"`
	Source            string `json:"source"`
	Status            string `json:"status"`
	Note              string `json:"note,omitempty"`
	SynchronizedAtMS  *int64 `json:"synchronized_at_ms,omitempty"`
}

func recordingDate(s string) (time.Time, bool) {
	t, err := time.Parse(time.RFC3339Nano, s)
	_, offset := t.Zone()
	return t, err == nil && t.Year() >= 2020 && t.Year() < 2100 && offset > -24*3600 && offset < 24*3600
}
func sessionTime(v Session) RecordingTime {
	if v.RecordingTime.Status != "" {
		return v.RecordingTime
	}
	m := v.Manifest.Time
	r := RecordingTime{OriginalStartedAt: m.Started, EndedAt: m.Ended, Timezone: m.Timezone, Source: "unknown", Status: "unknown", Note: "录制日期未知"}
	start, valid := recordingDate(m.Started)
	end, endValid := recordingDate(m.Ended)
	// Legacy correction requires a plausible end AND positive bounded duration;
	// never derive wall time from UUIDs, file mtimes or the export computer's clock.
	if endValid && m.Duration > 0 && m.Duration <= 7*24*3600 && !math.IsNaN(m.Duration) && !math.IsInf(m.Duration, 0) {
		if !valid || math.Abs(end.Sub(start).Seconds()-m.Duration) > 5 {
			corrected := end.Add(-time.Duration(m.Duration * float64(time.Second)))
			if _, ok := recordingDate(corrected.Format(time.RFC3339Nano)); ok {
				r.StartedAt = corrected.Format(time.RFC3339Nano)
				r.Status = "estimated"
				r.Note = "由结束时间和录制时长推算"
				return r
			}
		}
	}
	if valid {
		r.StartedAt = m.Started
		r.Status = "recorded"
		r.Note = "校时来源未知"
	}
	return r
}
func (v Session) TimeLabel() string {
	r := sessionTime(v)
	t, ok := recordingDate(r.StartedAt)
	if !ok {
		return "录制时间未知"
	}
	label := t.Format("2006-01-02 15:04:05")
	if r.Status == "estimated" {
		label += "（推算）"
	}
	return label
}
func (v Session) TimeDetail() string {
	r := sessionTime(v)
	if r.StartedAt == "" {
		return r.Note
	}
	return r.StartedAt + "\n" + r.Note
}
func loadRecordingTime(ctx context.Context, s source.Source, v *Session) {
	v.RecordingTime = sessionTime(*v)
	if ctx.Err() != nil {
		return
	}
	var artifact *Artifact
	for i := range v.Artifacts {
		if v.Artifacts[i].Path == "clock-calibration.json" {
			artifact = &v.Artifacts[i]
			break
		}
	}
	if artifact == nil {
		return
	}
	fail := func(reason string) {
		v.RecordingTime.StartedAt = ""
		v.RecordingTime.Status = "conflict"
		v.RecordingTime.Note = "校时记录异常：" + reason
	}
	if artifact.Bytes <= 0 || artifact.Bytes > 64<<10 {
		fail("大小无效")
		return
	}
	f, err := s.Open(path.Join(v.Dir, artifact.Path))
	if err != nil {
		fail(err.Error())
		return
	}
	b, err := io.ReadAll(io.LimitReader(f, (64<<10)+1))
	ce := f.Close()
	if err != nil || ce != nil || int64(len(b)) != artifact.Bytes {
		fail("读取失败或大小改变")
		return
	}
	h := sha256.Sum256(b)
	if hex.EncodeToString(h[:]) != artifact.SHA {
		fail("摘要不匹配")
		return
	}
	var c struct {
		Schema      string `json:"schema"`
		Session     string `json:"session_id"`
		Started     string `json:"started_at"`
		Offset      *int   `json:"utc_offset_seconds"`
		Calibration struct {
			Source       string `json:"source"`
			Synchronized *int64 `json:"synchronized_at_ms"`
		} `json:"calibration"`
	}
	if json.Unmarshal(b, &c) != nil || c.Schema != "openaria.recording-clock.v1" || c.Session != v.ID {
		fail("身份无效")
		return
	}
	start, ok := recordingDate(v.Manifest.Time.Started)
	cal, calOK := recordingDate(c.Started)
	_, offset := start.Zone()
	if !ok || !calOK || !start.Equal(cal) || c.Offset == nil || *c.Offset != offset {
		fail("与录制开始时间或时区不一致")
		return
	}
	if c.Calibration.Source != "client" && c.Calibration.Source != "ntp" {
		fail("未校时或来源无效")
		return
	}
	if c.Calibration.Synchronized == nil {
		fail("缺少校时日期")
		return
	}
	syncTime := time.UnixMilli(*c.Calibration.Synchronized)
	if syncTime.Year() < 2020 || syncTime.Year() >= 2100 || syncTime.After(start.Add(2*time.Second)) {
		fail("校时日期无效")
		return
	}
	end, endOK := recordingDate(v.Manifest.Time.Ended)
	duration := v.Manifest.Time.Duration
	if !endOK || math.IsNaN(duration) || math.IsInf(duration, 0) || duration < 0 || math.Abs(end.Sub(start).Seconds()-duration) > 5 {
		fail("起止时间与时长冲突")
		return
	}
	r := &v.RecordingTime
	r.StartedAt = v.Manifest.Time.Started
	r.Status = "calibrated"
	r.Source = c.Calibration.Source
	r.SynchronizedAtMS = c.Calibration.Synchronized
	if r.Source == "client" {
		r.Note = "手机/电脑校时"
	} else {
		r.Note = "NTP 校时"
	}
}
func sortRecordings(vs []Session) {
	sort.SliceStable(vs, func(i, j int) bool {
		a, aOK := recordingDate(sessionTime(vs[i]).StartedAt)
		b, bOK := recordingDate(sessionTime(vs[j]).StartedAt)
		if aOK != bOK {
			return aOK
		}
		if aOK && !a.Equal(b) {
			return a.Before(b)
		}
		return fmt.Sprint(vs[i].Manifest.VolumeID, "/", vs[i].ID) < fmt.Sprint(vs[j].Manifest.VolumeID, "/", vs[j].ID)
	})
}

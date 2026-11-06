package core

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"openaria-exporter/internal/source"
	"os"
	"path/filepath"
	"testing"
)

func completeFixture(t *testing.T, media bool) (source.Source, Session, map[string][]byte) {
	t.Helper()
	s, v := fixture(t, media)
	dir := t.TempDir()
	files := map[string][]byte{}
	original, err := inventory(t.Context(), s, v.Dir)
	if err != nil {
		t.Fatal(err)
	}
	for _, a := range original {
		r, e := s.Open(a.Path)
		if e != nil {
			t.Fatal(e)
		}
		b, e := io.ReadAll(r)
		r.Close()
		if e != nil {
			t.Fatal(e)
		}
		files[a.Path] = b
	}
	files["imu/imu.jsonl"] = []byte("{\"timestamp_ns\":123456789,\"accel\":[1,2,3],\"gyro\":[4,5,6]}\n")
	files["imu/frames.jsonl"] = []byte("{\"timestamp_ns\":123456789,\"frame\":0}\n")
	files["calibration/device.json"] = []byte("{\"raw_device_axes\":true}")
	files["extras/empty.bin"] = nil
	var m map[string]any
	if err = json.Unmarshal(v.Raw, &m); err != nil {
		t.Fatal(err)
	}
	for key, p := range map[string]string{"imu": "imu/imu.jsonl", "frames": "imu/frames.jsonl"} {
		h := sha256.Sum256(files[p])
		m[key] = map[string]any{"artifact": Artifact{Path: p, Bytes: int64(len(files[p])), SHA: hex.EncodeToString(h[:])}}
	}
	files["manifest.json"], err = json.Marshal(m)
	if err != nil {
		t.Fatal(err)
	}
	for p, b := range files {
		if err = os.MkdirAll(filepath.Dir(filepath.Join(dir, p)), 0700); err != nil {
			t.Fatal(err)
		}
		if err = os.WriteFile(filepath.Join(dir, p), b, 0600); err != nil {
			t.Fatal(err)
		}
	}
	fs, err := source.NewFolder(dir)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { fs.Close() })
	vs, err := Scan(t.Context(), fs)
	if err != nil || len(vs) != 1 || vs[0].Error != "" {
		t.Fatalf("scan: %v %+v", err, vs)
	}
	return fs, vs[0], files
}
func TestCompleteRecordingNormalAndFallback(t *testing.T) {
	requireMediaTools(t)
	for _, media := range []bool{true, false} {
		t.Run(map[bool]string{true: "processed", false: "fallback"}[media], func(t *testing.T) {
			s, v, files := completeFixture(t, media)
			dst, err := Export(t.Context(), s, v, Options{Output: t.TempDir(), VideoOnly: true, RawFallback: true})
			var warning *RawFallbackError
			if media && err != nil || !media && !errors.As(err, &warning) {
				t.Fatal(err)
			}
			for p, want := range files {
				got, e := os.ReadFile(filepath.Join(dst, "source", p))
				if e != nil || !bytes.Equal(got, want) {
					t.Fatalf("lost %s: %v", p, e)
				}
			}
			if !media {
				return
			}
			b, err := os.ReadFile(filepath.Join(dst, "export.json"))
			if err != nil {
				t.Fatal(err)
			}
			var rec struct {
				IMU   string           `json:"imu_file"`
				Audio []string         `json:"audio_files"`
				Files map[string]int64 `json:"source_files"`
			}
			if err = json.Unmarshal(b, &rec); err != nil {
				t.Fatal(err)
			}
			if rec.IMU != "source/imu/imu.jsonl" || len(rec.Audio) != 1 || len(rec.Files) != len(files) {
				t.Fatalf("receipt: %+v", rec)
			}
			out, err := InspectLocalOutput(dst, v)
			if err != nil {
				t.Fatal(err)
			}
			extra := filepath.Join(dst, "source", "user-notes.txt")
			if err = os.WriteFile(extra, []byte("keep"), 0600); err != nil {
				t.Fatal(err)
			}
			if err = DeleteLocalOutput(out); err == nil {
				t.Fatal("deleted extra user data")
			}
			if err = os.Remove(extra); err != nil {
				t.Fatal(err)
			}
			if err = DeleteLocalOutput(out); err != nil {
				t.Fatal(err)
			}
			if _, err = os.Stat(dst); !os.IsNotExist(err) {
				t.Fatal("directory remains", err)
			}
		})
	}
}
func TestCompleteRecordingCopyCancellation(t *testing.T) {
	s, v, _ := completeFixture(t, true)
	out := t.TempDir()
	ctx, cancel := context.WithCancel(t.Context())
	defer cancel()
	_, err := Export(ctx, s, v, Options{Output: out, VideoOnly: true, RawFallback: true, Progress: func(p Progress) {
		if p.Stage == "保存完整录制数据" {
			cancel()
		}
	}})
	if !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
	if _, err = os.Stat(filepath.Join(out, v.ID+"-video")); !os.IsNotExist(err) {
		t.Fatal("published incomplete data")
	}
	if _, err = os.Stat(filepath.Join(out, "."+v.ID+"-video.partial", "source")); !os.IsNotExist(err) {
		t.Fatal("retained incomplete data")
	}
}
func TestCompleteRecordingMissingArtifact(t *testing.T) {
	s, v, _ := completeFixture(t, false)
	v.Artifacts = append(v.Artifacts, Artifact{Path: "missing-imu.jsonl", Bytes: 1})
	_, err := Export(t.Context(), s, v, Options{Output: t.TempDir(), VideoOnly: true, RawFallback: true})
	var warning *RawFallbackError
	if err == nil || errors.As(err, &warning) {
		t.Fatal("missing data reported complete", err)
	}
}

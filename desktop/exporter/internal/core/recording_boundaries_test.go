package core

import (
	"bytes"
	"context"
	"errors"
	"io"
	"openaria-exporter/internal/source"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

type recordingFaultSource struct {
	source.Source
	file string
	mode string
}

func (s recordingFaultSource) Open(p string) (io.ReadCloser, error) {
	if p == s.file {
		switch s.mode {
		case "unplug":
			return nil, errors.New("source disconnected")
		case "short":
			return io.NopCloser(bytes.NewReader(nil)), nil
		case "long":
			return io.NopCloser(strings.NewReader(strings.Repeat("x", 2048))), nil
		}
	}
	return s.Source.Open(p)
}
func TestRecordingCopyFailuresDoNotPublish(t *testing.T) {
	for _, mode := range []string{"unplug", "short", "long"} {
		t.Run(mode, func(t *testing.T) {
			s, v, _ := completeFixture(t, true)
			out := t.TempDir()
			_, err := Export(t.Context(), recordingFaultSource{s, "imu/imu.jsonl", mode}, v, Options{Output: out, VideoOnly: true, RawFallback: true})
			var warning *RawFallbackError
			if err == nil || errors.As(err, &warning) {
				t.Fatal("incomplete data reported saved", err)
			}
			if _, err = os.Stat(filepath.Join(out, v.ID+"-video")); !os.IsNotExist(err) {
				t.Fatal("published incomplete data")
			}
		})
	}
}
func TestRecordingSourceMutationAndLinksBlockDelete(t *testing.T) {
	s, v, _ := completeFixture(t, true)
	for _, kind := range []string{"changed", "missing", "symlink", "empty-directory"} {
		t.Run(kind, func(t *testing.T) {
			dst, err := Export(t.Context(), s, v, Options{Output: t.TempDir(), VideoOnly: true})
			if err != nil {
				t.Fatal(err)
			}
			out, err := InspectLocalOutput(dst, v)
			if err != nil {
				t.Fatal(err)
			}
			p := filepath.Join(dst, "source", "imu", "imu.jsonl")
			switch kind {
			case "changed":
				err = os.WriteFile(p, []byte("changed"), 0600)
			case "missing":
				err = os.Remove(p)
			case "symlink":
				if err = os.Remove(p); err != nil {
					t.Fatal(err)
				}
				if err = os.Symlink(filepath.Join(dst, "video.mp4"), p); err != nil {
					t.Skip("symlinks unavailable", err)
				}
			case "empty-directory":
				err = os.Mkdir(filepath.Join(dst, "source", "notes"), 0700)
			}
			if err != nil {
				t.Fatal(err)
			}
			if err = DeleteLocalOutput(out); err == nil {
				t.Fatal("deleted changed recording")
			}
			if _, err = os.Stat(filepath.Join(dst, "video.mp4")); err != nil {
				t.Fatal("partially deleted despite failed preflight", err)
			}
		})
	}
}
func TestCompleteRecordingBatchConcurrent(t *testing.T) {
	s, v, _ := completeFixture(t, true)
	other := v
	other.ID = "second-complete"
	done := 0
	paths, err := ExportBatch(context.Background(), s, []Session{v, other}, Options{Output: t.TempDir(), batchTestWorkers: 2}, nil, func(int, string) { done++ })
	if err != nil || done != 2 || len(paths) != 2 {
		t.Fatal(paths, done, err)
	}
	for _, p := range paths {
		if _, err = os.Stat(filepath.Join(p, "source", "imu", "imu.jsonl")); err != nil {
			t.Fatal(err)
		}
	}
}

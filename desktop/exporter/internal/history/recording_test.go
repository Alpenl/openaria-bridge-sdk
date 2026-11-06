package history

import (
	"openaria-exporter/internal/core"
	"testing"
)

func TestCompleteRevisionIncludesIMUAndIsOrderIndependent(t *testing.T) {
	v := core.Session{Artifacts: []core.Artifact{{Path: "imu/imu.jsonl", Bytes: 5, SHA: "imu"}, {Path: "left.mp4", Bytes: 10, SHA: "video"}}}
	before := Revision(v)
	v.Artifacts[0], v.Artifacts[1] = v.Artifacts[1], v.Artifacts[0]
	if Revision(v) != before {
		t.Fatal("artifact ordering changed identity")
	}
	v.Artifacts[1].SHA = "different-imu"
	if Revision(v) == before {
		t.Fatal("IMU revision ignored")
	}
}

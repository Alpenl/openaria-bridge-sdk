package core

import (
	"context"
	"encoding/json"
	"os"
)

func mediaArtifacts(v Session) map[string]Artifact {
	out := map[string]Artifact{}
	canonical := map[string]Artifact{}
	for _, a := range v.Artifacts {
		canonical[a.Path] = a
	}
	add := func(a Artifact) {
		if normalized, ok := canonical[a.Path]; ok {
			a = normalized
		}
		out[a.Path] = a
	}
	for _, seg := range v.Manifest.Video.Segments {
		add(seg.Artifacts.Left)
		add(seg.Artifacts.Right)
	}
	for _, seg := range v.Manifest.Audio.Segments {
		add(seg.Artifact)
	}
	return out
}
func MediaBytes(v Session) int64 {
	var n int64
	for _, a := range mediaArtifacts(v) {
		n += a.Bytes
	}
	return n
}
func finishVideo(ctx context.Context, st *os.Root, v Session, manifestSHA string) error {
	if e := st.Rename("playback.mp4", "video.mp4"); e != nil {
		return e
	}
	f, e := st.OpenFile("video.mp4", os.O_RDWR, 0)
	if e != nil {
		return e
	}
	if e = f.Sync(); e != nil {
		f.Close()
		return e
	}
	info, e := f.Stat()
	ce := f.Close()
	if e != nil {
		return e
	}
	if ce != nil {
		return ce
	}
	if e = ctx.Err(); e != nil {
		return e
	}
	b, e := json.MarshalIndent(map[string]any{"session_id": v.ID, "display_name": v.Name, "source_manifest_sha256": manifestSHA, "file": "video.mp4", "bytes": info.Size(), "media_hash_verified": false, "format": "H.264 SBS MP4", "source_copies_retained": false}, "", "  ")
	if e != nil {
		return e
	}
	return writeNew(st, "export.json", b)
}

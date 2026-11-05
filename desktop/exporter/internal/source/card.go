package source

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"regexp"
	"time"
)

var ErrNotCard = errors.New("未识别到 OpenAria 录制服务")
var volumeUUID = regexp.MustCompile(`^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$`)

type CardIdentity struct {
	VolumeID      string
	InitializedAt string
}

// IdentifyCard checks the on-disk capture-volume protocol, not a generic ext4
// signature or a user-facing volume name. This is format recognition, not proof
// of cryptographic authenticity. It also admits empty, initialized cards.
func IdentifyCard(s Source) (CardIdentity, error) {
	r, e := s.Open(".ylx-volume.json")
	if e != nil {
		if errors.Is(e, os.ErrNotExist) {
			return CardIdentity{}, ErrNotCard
		}
		return CardIdentity{}, e
	}
	b, e := io.ReadAll(io.LimitReader(r, 4097))
	ce := r.Close()
	if e != nil {
		return CardIdentity{}, e
	}
	if ce != nil {
		return CardIdentity{}, ce
	}
	var m map[string]json.RawMessage
	if len(b) > 4096 || json.Unmarshal(b, &m) != nil || len(m) != 3 {
		return CardIdentity{}, fmt.Errorf("%w：录制卷标记无效", ErrNotCard)
	}
	var schema, id, created string
	if json.Unmarshal(m["schema"], &schema) != nil || json.Unmarshal(m["volume_id"], &id) != nil || json.Unmarshal(m["initialized_at"], &created) != nil || schema != "ylx.capture-volume.v1" || !volumeUUID.MatchString(id) {
		return CardIdentity{}, fmt.Errorf("%w：不支持的录制卷协议", ErrNotCard)
	}
	if _, e = time.Parse(time.RFC3339Nano, created); e != nil {
		return CardIdentity{}, fmt.Errorf("%w：初始化时间无效", ErrNotCard)
	}
	// Listing the root avoids following a recordings/sessions symlink. Detailed
	// contents are read only by the subsequent scan.
	entries, e := s.List(".")
	if e != nil {
		return CardIdentity{}, e
	}
	for _, v := range entries {
		if v.Dir && (v.Name == "recordings" || v.Name == "sessions") {
			return CardIdentity{id, created}, nil
		}
	}
	return CardIdentity{}, fmt.Errorf("%w：缺少录像目录", ErrNotCard)
}

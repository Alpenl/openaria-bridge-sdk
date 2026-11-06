package core

import (
	"fmt"
	"strings"
)

func stackFailureKey(p encoderProfile, codec string, w, h int) string {
	return fmt.Sprintf("%s/%v/%s/%dx%d", p.stack, p.global, codec, w, h)
}
func (g *mediaStream) stackFailed(key string) bool {
	g.mu.Lock()
	defer g.mu.Unlock()
	return g.failedStacks[key]
}
func (g *mediaStream) recordStackFailure(key string, err error) {
	if err == nil {
		return
	}
	text := strings.ToLower(err.Error())
	// Do not poison a hardware capability on cancellation, source/target I/O or
	// timeline errors. Cache only explicit frame-pool/texture incompatibility.
	if !strings.Contains(text, "could not create the texture") && !strings.Contains(text, "error creating frames_ctx") {
		return
	}
	g.mu.Lock()
	defer g.mu.Unlock()
	if g.failedStacks == nil {
		g.failedStacks = map[string]bool{}
	}
	g.failedStacks[key] = true
}

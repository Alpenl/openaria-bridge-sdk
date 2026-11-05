package core

import (
	"log"
	"time"
)

// Worker fields are read only after join. Durations overlap and must not be summed
// to infer wall time; copy includes source reads, hashing and destination writes.
type sessionTimings struct {
	session                                             string
	serial                                              bool
	start                                               time.Time
	copy, sync, verify, inputProbe, encode, outputProbe time.Duration
}

func newSessionTimings(id string, serial bool) *sessionTimings {
	return &sessionTimings{session: id, serial: serial, start: time.Now()}
}
func (s *sessionTimings) log() {
	log.Printf("export timing session=%q serial=%t total=%s copy=%s sync_close=%s target_verify=%s input_probe=%s encode=%s output_probe=%s", s.session, s.serial, time.Since(s.start), s.copy, s.sync, s.verify, s.inputProbe, s.encode, s.outputProbe)
}

package main

import (
	"context"
	"github.com/egoist/mygo"
	"github.com/egoist/mygo/ui"
	"openaria-exporter/internal/source"
	"testing"
)

func TestBusyExitProtection(t *testing.T) {
	for _, export := range []bool{false, true} {
		s, e := source.NewFolder(t.TempDir())
		if e != nil {
			t.Fatal(e)
		}
		ctx, cancel := context.WithCancel(context.Background())
		defer cancel()
		a := &app{busy: true, exporting: export, held: s, cancel: cancel}
		q := &mygo.QuitEvent{}
		a.beforeQuit(q)
		c := &mygo.CloseEvent{}
		a.beforeClose(c)
		if !q.DefaultPrevented() || !c.DefaultPrevented() || ctx.Err() != nil {
			t.Fatal("busy exit must be prevented without cancellation")
		}
		if _, e = s.List("."); e != nil {
			t.Fatal("source closed before worker cleanup")
		}
		// Only the user's Cancel action may cancel; cancellation alone does not
		// mean the worker has finished cleaning up.
		tt := ui.NewTester(a.view, 1000, 850)
		if err := tt.Click("取消"); err != nil {
			t.Fatal(err)
		}
		if ctx.Err() != context.Canceled {
			t.Fatal("explicit cancellation not delivered")
		}
		waiting := &mygo.QuitEvent{}
		a.beforeQuit(waiting)
		if !waiting.DefaultPrevented() {
			t.Fatal("quit allowed before busy cleared")
		}
		if _, err := s.List("."); err != nil {
			t.Fatal("source closed during cleanup")
		}
		a.busy = false
		done := &mygo.QuitEvent{}
		a.beforeQuit(done)
		if done.DefaultPrevented() {
			t.Fatal("idle quit prevented")
		}
		if a.held != nil {
			t.Fatal("source not released")
		}
	}
}
func TestUpdateUnavailable(t *testing.T) {
	seen := map[string]bool{}
	for _, args := range [][3]bool{{true, false, false}, {false, false, false}, {false, true, false}} {
		s := updateUnavailable(args[0], args[1], args[2])
		if s == "" || seen[s] {
			t.Fatal("indistinct unavailable state")
		}
		seen[s] = true
	}
	if updateUnavailable(false, true, true) != "" {
		t.Fatal("enabled")
	}
}

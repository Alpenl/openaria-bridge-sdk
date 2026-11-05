package main

import "testing"

func TestElevationDecision(t *testing.T) {
	for _, c := range []struct {
		e, m bool
		want int
	}{{false, false, 1}, {false, true, -1}, {true, false, 0}, {true, true, 0}} {
		if got := elevationDecision(c.e, c.m); got != c.want {
			t.Fatalf("%+v got %d", c, got)
		}
	}
}

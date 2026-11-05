package main

import (
	"context"
	"errors"
	"io"
	"openaria-exporter/internal/core"
	"openaria-exporter/internal/source"
	"os"
	"strings"
	"testing"
)

const testCardID = "12345678-1234-4123-8123-123456789abc"

type probeCard struct {
	id     string
	closed bool
}

func (s *probeCard) Open(p string) (io.ReadCloser, error) {
	if s.id == "" {
		return nil, os.ErrNotExist
	}
	return io.NopCloser(strings.NewReader(`{"schema":"ylx.capture-volume.v1","volume_id":"` + s.id + `","initialized_at":"2026-11-04T00:00:00Z"}`)), nil
}
func (s *probeCard) List(string) ([]source.Entry, error) {
	return []source.Entry{{Name: "recordings", Dir: true}}, nil
}
func (s *probeCard) Close() error { s.closed = true; return nil }
func cardHooks(t *testing.T) {
	t.Helper()
	enumerate, open := cardEnumerate, cardOpen
	t.Cleanup(func() { cardEnumerate, cardOpen = enumerate, open })
}
func TestCardDiscovery(t *testing.T) {
	cardHooks(t)
	cardEnumerate = func(context.Context) (cardInventory, error) {
		return cardInventory{External: 3, Candidates: []Device{{"b", "32 GiB"}, {"a", "64 GiB"}, {"c", "16 GiB"}}}, nil
	}
	var probes []*probeCard
	cardOpen = func(_ context.Context, p string, interactive bool) (source.Source, error) {
		if interactive {
			t.Fatal("background requested authorization")
		}
		if p == "c" {
			return nil, os.ErrPermission
		}
		s := &probeCard{}
		if p == "a" {
			s.id = testCardID
		}
		probes = append(probes, s)
		return s, nil
	}
	r := discoverCards(context.Background(), false)
	if len(r.Cards) != 1 || r.Cards[0].Path != "a" || !errors.Is(r.Err, os.ErrPermission) {
		t.Fatalf("%+v", r)
	}
	for _, s := range probes {
		if !s.closed {
			t.Fatal("leaked probe")
		}
	}
	cardEnumerate = func(context.Context) (cardInventory, error) { return cardInventory{External: 1}, nil }
	r = discoverCards(context.Background(), false)
	if !strings.Contains(r.Message, "未识别到") || r.Err != nil {
		t.Fatalf("%+v", r)
	}
	cardEnumerate = func(context.Context) (cardInventory, error) { return cardInventory{}, nil }
	r = discoverCards(context.Background(), false)
	if !strings.Contains(r.Message, "未检测到") {
		t.Fatalf("%+v", r)
	}
}
func TestCardSelectionAndRemoval(t *testing.T) {
	first := Card{Path: "a", VolumeID: testCardID}
	second := Card{Path: "b", VolumeID: "22345678-1234-4123-8123-123456789abc"}
	a := &app{}
	if !a.applyCards(cardDiscovery{Cards: []Card{first, second}}) || a.src != "a" {
		t.Fatal("no default first")
	}
	// Simulate an already scanned, deliberately selected second card.
	s := &probeCard{}
	a.src = "b"
	a.cardID = second.VolumeID
	a.held = s
	a.scanned = "b"
	a.items = []core.Session{{ID: "keep"}}
	a.picked = map[string]bool{"keep": true}
	if a.applyCards(cardDiscovery{Cards: []Card{first, second}}) || s.closed || len(a.items) != 1 {
		t.Fatal("selection not preserved")
	}
	if !a.applyCards(cardDiscovery{Cards: []Card{first}}) || !s.closed || len(a.items) != 0 || len(a.picked) != 0 || a.scanned != "" || a.src != "a" {
		t.Fatal("removal retained old state")
	}
	s = &probeCard{}
	a.held = s
	replacement := first
	replacement.VolumeID = second.VolumeID
	if !a.applyCards(cardDiscovery{Cards: []Card{replacement}}) || !s.closed || a.cardID != replacement.VolumeID {
		t.Fatal("same-path replacement not detected")
	}
	a.applyCards(cardDiscovery{})
	if a.src != "" || a.cardID != "" || a.held != nil {
		t.Fatal("unplug did not clear")
	}
}
func TestOpenSelectedCardRejectsLocalAndReplacement(t *testing.T) {
	cardHooks(t)
	cardEnumerate = func(context.Context) (cardInventory, error) {
		return cardInventory{Candidates: []Device{{"external", "32 GiB"}}}, nil
	}
	opened := 0
	var probe *probeCard
	cardOpen = func(context.Context, string, bool) (source.Source, error) {
		opened++
		probe = &probeCard{id: testCardID}
		return probe, nil
	}
	if _, e := openSelectedCard(context.Background(), "/tmp/local", ""); e == nil || opened != 0 {
		t.Fatal("local source opened")
	}
	if _, e := openSelectedCard(context.Background(), "external", "changed"); e == nil || !probe.closed {
		t.Fatal("replacement admitted or leaked")
	}
	s, e := openSelectedCard(context.Background(), "external", testCardID)
	if e != nil || probe.closed {
		t.Fatalf("valid card: %v", e)
	}
	s.Close()
}
func TestNoRefreshWhileBusyOrStopped(t *testing.T) {
	cardHooks(t)
	cardEnumerate = func(context.Context) (cardInventory, error) {
		t.Error("unexpected enumeration")
		return cardInventory{}, nil
	}
	for _, a := range []*app{{busy: true}, {refreshing: true}, {cardsStopped: true}} {
		a.refreshCards(false)
	}
}

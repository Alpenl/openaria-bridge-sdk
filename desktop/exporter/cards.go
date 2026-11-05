package main

import (
	"context"
	"errors"
	"fmt"
	"log"
	"openaria-exporter/internal/core"
	"openaria-exporter/internal/source"
	"sort"
	"strings"
	"time"
)

type cardInventory struct {
	Candidates []Device
	External   int
}
type Card struct{ Path, Label, VolumeID string }
type cardDiscovery struct {
	Cards   []Card
	Message string
	Err     error
}

var cardEnumerate = externalPartitions
var cardOpen = func(ctx context.Context, p string, interactive bool) (source.Source, error) {
	if interactive {
		return authorizedSource(ctx, p)
	}
	return passiveCardSource(ctx, p)
}

func discoverCards(ctx context.Context, interactive bool) cardDiscovery {
	inv, e := cardEnumerate(ctx)
	if e != nil {
		return cardDiscovery{Message: "暂时无法检测内存卡，请稍后重试。", Err: e}
	}
	sort.Slice(inv.Candidates, func(i, j int) bool { return inv.Candidates[i].Path < inv.Candidates[j].Path })
	var cards []Card
	var readErr error
	rejected := 0
	for _, d := range inv.Candidates {
		if e = ctx.Err(); e != nil {
			readErr = e
			break
		}
		s, err := cardOpen(ctx, d.Path, interactive)
		if err != nil {
			if strings.Contains(err.Error(), "不是 ext4") {
				rejected++
				continue
			}
			readErr = err
			continue
		}
		id, err := source.IdentifyCard(s)
		closeErr := s.Close()
		if errors.Is(err, source.ErrNotCard) {
			rejected++
			continue
		}
		if err != nil {
			readErr = err
			continue
		}
		if closeErr != nil {
			readErr = closeErr
			continue
		}
		cards = append(cards, Card{d.Path, fmt.Sprintf("内存卡 %d · %s · %s", len(cards)+1, d.Label, id.VolumeID[:8]), id.VolumeID})
	}
	msg := "未检测到内存卡，请插入 OpenAria 内存卡。"
	if len(cards) > 0 {
		msg = fmt.Sprintf("已识别 %d 张 OpenAria 内存卡 · 自动检测插拔", len(cards))
		if rejected > 0 || inv.External > len(cards) {
			msg += "；其他分区未识别到 OpenAria 录制服务"
		}
	}
	if len(cards) == 0 && inv.External > 0 {
		msg = "检测到外置存储，但未识别到 OpenAria 录制服务。请插入 OpenAria 内存卡。"
	}
	if readErr != nil {
		msg = "内存卡暂时无法读取。请检查读卡器或设备访问授权，再点击重新检测。"
		if len(cards) > 0 {
			msg = "部分设备无法读取；已识别的 OpenAria 内存卡仍可使用。"
		}
	}
	return cardDiscovery{cards, msg, readErr}
}

// Every explicit scan rechecks external-device membership and the protocol.
// A path typed into a CLI is not enough to admit a folder or disk image.
func openSelectedCard(ctx context.Context, p, expectedID string) (source.Source, error) {
	inv, e := cardEnumerate(ctx)
	if e != nil {
		return nil, e
	}
	found := false
	for _, d := range inv.Candidates {
		if d.Path == p {
			found = true
			break
		}
	}
	if !found {
		return nil, fmt.Errorf("内存卡已拔出或不再可用，请重新检测")
	}
	s, e := cardOpen(ctx, p, true)
	if e != nil {
		return nil, e
	}
	id, e := source.IdentifyCard(s)
	if e != nil {
		s.Close()
		return nil, e
	}
	if expectedID != "" && id.VolumeID != expectedID {
		s.Close()
		return nil, fmt.Errorf("内存卡已更换，请重新检测")
	}
	return s, nil
}
func scanCard(ctx context.Context, s source.Source) ([]core.Session, error) {
	id, err := source.IdentifyCard(s)
	if err != nil {
		return nil, err
	}
	items, err := core.Scan(ctx, s)
	for i := range items {
		if items[i].Manifest.VolumeID != id.VolumeID {
			items[i].Error = "录像所属录制卷与当前内存卡不一致"
		}
	}
	return items, err
}

func (a *app) applyCards(r cardDiscovery) bool {
	a.cards = r.Cards
	a.cardMessage = r.Message
	if r.Err != nil {
		log.Printf("card detection: %v", r.Err)
	}
	for _, c := range a.cards {
		if c.Path == a.src && c.VolumeID == a.cardID {
			a.cardChoice = c.Label
			return false
		}
	}
	a.changeSource("")
	a.cardID = ""
	a.cardChoice = ""
	if len(a.cards) == 0 {
		return false
	}
	a.src = a.cards[0].Path
	a.cardID = a.cards[0].VolumeID
	a.cardChoice = a.cards[0].Label
	return true
}
func (a *app) selectCard(c Card) {
	if a.busy || a.refreshing {
		return
	}
	if a.src == c.Path && a.cardID == c.VolumeID {
		return
	}
	a.changeSource(c.Path)
	a.cardID = c.VolumeID
	a.cardChoice = c.Label
	a.run(false)
}
func (a *app) refreshCards(interactive bool) {
	if a.busy || a.refreshing || a.cardsStopped {
		return
	}
	a.refreshing = true
	parent := a.cardsContext
	if parent == nil {
		parent = context.Background()
	}
	go func() {
		timeout := 20 * time.Second
		if interactive {
			timeout = 2 * time.Minute
		}
		ctx, cancel := context.WithTimeout(parent, timeout)
		defer cancel()
		r := discoverCards(ctx, interactive)
		a.update(func() {
			a.refreshing = false
			if a.cardsStopped || a.busy {
				return
			}
			if a.applyCards(r) {
				a.run(false)
			} else if a.src == "" {
				a.setStatus(a.cardMessage, r.Err != nil)
			}
		})
	}()
}
func (a *app) startCardMonitor() {
	ctx, cancel := context.WithCancel(context.Background())
	a.cardsContext = ctx
	a.cardsCancel = cancel
	a.refreshCards(false)
	go func() {
		ticker := time.NewTicker(3 * time.Second)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				a.update(func() { a.refreshCards(false) })
			}
		}
	}()
}
func (a *app) stopCardMonitor() {
	a.cardsStopped = true
	if a.cardsCancel != nil {
		a.cardsCancel()
	}
}

package main

import (
	"context"
	"encoding/json"
	"fmt"
	"openaria-exporter/internal/source"
	"os/exec"
)

func externalPartitions(ctx context.Context) (cardInventory, error) {
	b, e := exec.CommandContext(ctx, "lsblk", "-J", "-b", "-p", "-o", "NAME,TYPE,TRAN,RM,FSTYPE,SIZE").Output()
	if e != nil {
		return cardInventory{}, e
	}
	type node struct {
		Name, Type, Tran, Fstype string
		RM                       bool
		Size                     json.Number
		Children                 []json.RawMessage
	}
	var root struct{ Blockdevices []json.RawMessage }
	if e = json.Unmarshal(b, &root); e != nil {
		return cardInventory{}, e
	}
	out := cardInventory{}
	var walk func(json.RawMessage, bool, int64)
	walk = func(raw json.RawMessage, external bool, size int64) {
		var n node
		if json.Unmarshal(raw, &n) != nil {
			return
		}
		external = external || n.RM || n.Tran == "usb"
		if n.Type == "disk" && external {
			out.External++
			size, _ = n.Size.Int64()
		}
		if n.Type == "part" && external && n.Fstype == "ext4" {
			out.Candidates = append(out.Candidates, Device{n.Name, fmt.Sprintf("%.1f GiB", float64(size)/(1<<30))})
		}
		for _, c := range n.Children {
			walk(c, external, size)
		}
	}
	for _, n := range root.Blockdevices {
		walk(n, false, 0)
	}
	pruneLinuxCards(out.Candidates)
	return out, nil
}

// Background detection must never start repeated polkit password dialogs.
func passiveCardSource(ctx context.Context, p string) (source.Source, error) {
	s, e := source.Open(p)
	if e == nil {
		return s, nil
	}
	if cached, err := retainedLinuxCard(p); err == nil {
		return cached, nil
	}
	return nil, e
}

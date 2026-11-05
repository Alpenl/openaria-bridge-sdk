package main

import (
	"bytes"
	"encoding/json"
)

type winPartition struct {
	Disk, Partition uint32
	Offset, Size    int64
	Name            string
}

func parseWinPartitions(b []byte) ([]winPartition, error) {
	b = bytes.TrimSpace(b)
	if len(b) == 0 || bytes.Equal(b, []byte("null")) {
		return nil, nil
	}
	if b[0] == '{' {
		var p winPartition
		e := json.Unmarshal(b, &p)
		if e != nil {
			return nil, e
		}
		return []winPartition{p}, nil
	}
	var ps []winPartition
	e := json.Unmarshal(b, &ps)
	return ps, e
}

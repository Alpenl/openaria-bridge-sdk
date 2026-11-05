package main

import (
	"context"
	"fmt"
	"openaria-exporter/internal/source"
	"os"
	"strings"
)

type Device struct{ Path, Label string }

func openSource(ctx context.Context, p string) (source.Source, error) {
	if strings.HasPrefix(p, "usb-disk:") {
		return authorizedSource(ctx, p)
	}
	s, e := os.Stat(p)
	if e != nil {
		return nil, e
	}
	if s.Mode()&os.ModeDevice != 0 {
		return authorizedSource(ctx, p)
	}
	if p == "" {
		return nil, fmt.Errorf("请选择源")
	}
	return source.Open(p)
}

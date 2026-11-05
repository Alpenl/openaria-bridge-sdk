//go:build e2e

package main

import (
	"context"
	"encoding/json"
	_ "github.com/egoist/mygo/plugins/automation"
	"openaria-exporter/internal/source"
	"os"
)

// Only e2e builds can simulate hotplug; shipping builds never expose local sources.
func init() {
	if p := os.Getenv("OPENARIA_E2E_HISTORY"); p != "" {
		localHistoryPath = func() (string, error) { return p, nil }
	}
	control := os.Getenv("OPENARIA_E2E_CARDS")
	if control == "" {
		return
	}
	cardEnumerate = func(context.Context) (cardInventory, error) {
		b, e := os.ReadFile(control)
		if e != nil {
			return cardInventory{}, e
		}
		var inv cardInventory
		e = json.Unmarshal(b, &inv)
		return inv, e
	}
	cardOpen = func(_ context.Context, p string, _ bool) (source.Source, error) { return source.Open(p) }
}

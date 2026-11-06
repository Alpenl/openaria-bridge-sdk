package main

import (
	"context"
	"openaria-exporter/internal/source"
)

func externalPartitions(ctx context.Context) (cardInventory, error) {
	inv, _, err := nativeWinInventory(ctx)
	return inv, err
}
func passiveCardSource(ctx context.Context, p string) (source.Source, error) {
	return authorizedSource(ctx, p)
}

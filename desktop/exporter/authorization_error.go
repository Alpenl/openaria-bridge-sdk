package main

import (
	"context"
	"fmt"
	"log"
)

func authorizationError(ctx context.Context, err error) error {
	if err == nil {
		return nil
	}
	log.Printf("stage=source-authorization reason=%v", err)
	if ctx.Err() != nil {
		return fmt.Errorf("读取已取消。%s", logLocation())
	}
	return fmt.Errorf("设备只读访问失败：%v%s。%s", err, devicePrivacyHint(err), logLocation())
}

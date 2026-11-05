package process

import (
	"context"
	"testing"
)

func TestBackgroundCommandCapturesOutput(t *testing.T) {
	cmd := CommandContext(context.Background(), "cmd.exe", "/d", "/c", "echo openaria-background")
	if cmd.SysProcAttr == nil || !cmd.SysProcAttr.HideWindow || cmd.SysProcAttr.CreationFlags&0x08000000 == 0 {
		t.Fatal("background command would allocate a console window")
	}
	out, err := cmd.Output()
	if err != nil || string(out) != "openaria-background\r\n" {
		t.Fatalf("output=%q err=%v", out, err)
	}
}

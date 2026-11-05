package process

import (
	"os/exec"
	"syscall"
)

func configure(cmd *exec.Cmd) {
	// CREATE_NO_WINDOW suppresses console allocation; redirected output and
	// CommandContext cancellation remain available for diagnostics and cleanup.
	cmd.SysProcAttr = &syscall.SysProcAttr{HideWindow: true, CreationFlags: 0x08000000}
}

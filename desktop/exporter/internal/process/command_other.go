//go:build !windows

package process

import "os/exec"

func configure(cmd *exec.Cmd) {}

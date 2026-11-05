//go:build !windows

package main

import (
	"fmt"
	"os"
)

func ensureElevated() (bool, error) { return false, nil }

func startupError(err error) { fmt.Fprintln(os.Stderr, err) }

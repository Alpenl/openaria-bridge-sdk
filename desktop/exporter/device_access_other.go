//go:build !darwin

package main

import "github.com/egoist/mygo"

func devicePrivacyHint(error) string              { return "" }
func (a *app) deviceAccessMenu() []*mygo.MenuItem { return nil }
func (a *app) firstDeviceAccessGuide()            {}

package main

import "github.com/egoist/mygo"

// All lifecycle state belongs to the UI thread. Never close a source while a
// worker still uses it; the worker completion owns cleanup after cancellation.
func (a *app) protectExit(prevent func()) {
	if a.busy {
		prevent()
		a.setStatus("任务进行中，请等待完成；如需取消，请点击取消并等待清理后再关闭或退出。", false)
		return
	}
	a.stopCardMonitor()
	if a.held != nil {
		a.held.Close()
		a.held = nil
	}
}
func (a *app) beforeQuit(e *mygo.QuitEvent)   { a.protectExit(e.PreventDefault) }
func (a *app) beforeClose(e *mygo.CloseEvent) { a.protectExit(e.PreventDefault) }

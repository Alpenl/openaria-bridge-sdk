package main

import (
	"fmt"
	"io"
	"os"
	"runtime"

	"github.com/egoist/mygo"
)

func (a *app) exportDiagnostics() {
	go func() {
		if userLog == nil {
			a.notice("诊断信息不可用", logLocation())
			return
		}
		path, err := mygo.Dialog.Save(mygo.SaveDialogOptions{Title: "保存诊断信息", DefaultPath: "OpenAria-diagnostics.txt", Message: "包含应用日志（可能含录像名称和路径），不包含录像内容，不会自动上传。"})
		if err != nil {
			a.notice("无法保存诊断信息", err.Error())
			return
		}
		if path == "" {
			return
		}
		// Refuse to overwrite an existing file, even after a save-dialog prompt.
		out, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
		if err != nil {
			a.notice("无法保存诊断信息", "请选择新的文件名。\n"+err.Error())
			return
		}
		defer out.Close()
		_, err = fmt.Fprintf(out, "Open Aria Exporter %s\nOS: %s/%s\n不包含录像内容；日志可能含名称及路径，请检查后再分享。\n\n", mygo.App.Version(), runtime.GOOS, runtime.GOARCH)
		if err == nil {
			var src *os.File
			src, err = os.Open(userLogPath)
			if err == nil {
				_, err = io.Copy(out, io.LimitReader(src, 2<<20))
				src.Close()
			}
		}
		if err == nil {
			err = out.Sync()
		}
		if err != nil {
			a.notice("诊断信息保存失败", err.Error())
			return
		}
		a.notice("诊断信息已保存", path+"\n请检查内容后再发送给支持人员。")
	}()
}

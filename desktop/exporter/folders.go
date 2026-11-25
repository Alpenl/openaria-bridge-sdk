package main

import (
	"context"
	"fmt"
	"os"

	"github.com/egoist/mygo"
	"openaria-exporter/internal/core"
	"openaria-exporter/internal/source"
)

// Folder imports need only the recording manifests, never a card identity or
// the device's SQLite database. This also supports OS-mounted exFAT cards.
func openRecordingSource(ctx context.Context, p, cardID string, folder bool) (source.Source, error) {
	if folder {
		return source.NewFolder(p)
	}
	return openSelectedCard(ctx, p, cardID)
}

func scanRecordingSource(ctx context.Context, s source.Source, folder bool) ([]core.Session, error) {
	if folder {
		items, err := core.ScanFolder(ctx, s)
		if err == nil && len(items) == 0 {
			return nil, fmt.Errorf("未找到录像，请选择包含 manifest.json 的录像文件夹、日期文件夹或 recordings 文件夹")
		}
		return items, err
	}
	return scanCard(ctx, s)
}

func isSourceFolder(p string) bool {
	st, err := os.Stat(p)
	return err == nil && st.IsDir()
}

func (a *app) selectFolder(p string) {
	a.changeSource(p)
	a.folderSource = true
	a.cardID, a.cardChoice = "", ""
}

func (a *app) chooseSourceFolder() {
	if a.busy || a.refreshing {
		return
	}
	a.busy = true
	go func() {
		paths, err := mygo.Dialog.Open(mygo.OpenDialogOptions{Directory: true})
		a.update(func() {
			a.busy = false
			if err != nil {
				a.setStatus(err.Error(), true)
			} else if len(paths) > 0 {
				a.selectFolder(paths[0])
				a.run(false)
			}
		})
	}()
}

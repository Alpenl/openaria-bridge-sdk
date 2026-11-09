package main

import "openaria-exporter/internal/core"

// ExportBatch returns a nonempty path for either processed success (which calls
// complete) or a published raw fallback. Keep these results out of history marks.
func (a *app) applyExportResults(selected []core.Session, paths []string, completed map[string]bool) {
	if a.exportResults == nil {
		a.exportResults = map[string]string{}
	}
	for i, v := range selected {
		result := "本次未完成"
		if completed[v.Dir] {
			result = "本次已导出"
		} else if i < len(paths) && paths[i] != "" {
			result = "本次仅保存原始数据"
		}
		a.exportResults[v.Dir] = result
	}
}

package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"github.com/egoist/mygo"
	"github.com/egoist/mygo/ui"
	"log"
	"openaria-exporter/internal/core"
	"openaria-exporter/internal/source"
	"os"
	"os/signal"
	"runtime"
	"strings"
)

type app struct {
	src, out, query, status         string
	busy                            bool
	items                           []core.Session
	picked                          map[string]bool
	list                            ui.ListState
	cancel                          context.CancelFunc
	w                               *mygo.Window
	cards                           []Card
	cardID, cardMessage, cardChoice string
	refreshing, cardsStopped        bool
	cardsContext                    context.Context
	cardsCancel                     context.CancelFunc
	scanned                         string
	held                            source.Source
	localAction                     *localAction
	// exporting 区分扫描与导出；progress 为批量导出进度，负数表示时长未知。
	exporting, failed, succeeded bool
	progress                     float64
}

func (a *app) update(f func()) {
	if a.w != nil {
		a.w.Update(f)
	} else {
		f()
	}
}
func (a *app) chooseOutput() {
	if a.busy || a.refreshing {
		return
	}
	a.busy = true
	go func() {
		paths, e := mygo.Dialog.Open(mygo.OpenDialogOptions{Directory: true})
		a.update(func() {
			a.busy = false
			if e != nil {
				a.setStatus(e.Error(), true)
			} else if len(paths) > 0 {
				if err := a.setOutputBase(paths[0]); err != nil {
					a.setStatus("保存位置未能保存："+err.Error(), true)
				}
			}
		})
	}()
}
func (a *app) run(export bool) {
	if a.busy || a.refreshing || a.src == "" || export && (a.held == nil || a.scanned != a.src) {
		return
	}
	if !export {
		a.changeSource(a.src)
	}
	ctx, cancel := context.WithCancel(context.Background())
	a.cancel = cancel
	a.busy = true
	a.exporting = export
	a.progress = 0
	a.failed, a.succeeded = false, false
	a.status = "正在扫描…"
	if export {
		a.status = "准备导出…"
	}
	src, cardID, out := a.src, a.cardID, outputDirectory(a.out)
	var selected []core.Session
	for _, v := range a.items {
		if a.picked[v.Dir] {
			selected = append(selected, v)
		}
	}
	held := a.held
	go func() {
		defer cancel()
		s := held
		var e error
		if !export {
			s, e = openSelectedCard(ctx, src, cardID)
		}
		var items []core.Session
		msg := ""
		var historyErr error
		if e == nil {

			if !export {
				items, e = scanCard(ctx, s)
				msg = fmt.Sprintf("发现 %d 条录像。导出将合并分段并生成 MP4。", len(items)) + source.Warnings(s)
				if e == nil {
					historyErr = loadLocalMarks(items)
				}
			} else {
				var batchTotal, completedBytes int64
				for _, v := range selected {
					batchTotal += core.MediaBytes(v)
				}
				completed := 0
				_, e = core.ExportBatch(ctx, s, selected, core.Options{Output: out, VideoOnly: true}, func(index int, p core.Progress) {
					text := progressText(p, index+1, len(selected), completedBytes, batchTotal)
					a.update(func() { a.status = text; a.progress = -1 })
				}, func(index int, dst string) {
					v := selected[index]
					mark, markErr := saveLocalMark(v, dst)
					if markErr != nil {
						historyErr = errors.Join(historyErr, fmt.Errorf("%s：视频已导出，但本机标记保存失败：%w", v.Name, markErr))
					} else {
						a.update(func() {
							for i := range a.items {
								if a.items[i].Dir == v.Dir {
									a.items[i].LocalExportedAt = mark.ExportedAt
									a.items[i].LocalExportPath = mark.Output
								}
							}
						})
					}
					completed++
					completedBytes += core.MediaBytes(selected[index])
					msg = fmt.Sprintf("已完成 %d/%d 条 · %.1f/%.1f MiB · %s", completed, len(selected), float64(completedBytes)/(1<<20), float64(batchTotal)/(1<<20), dst)
				})
			}
		}
		a.update(func() {
			a.busy = false
			a.exporting = false
			a.cancel = nil

			if e != nil {
				a.setStatus(e.Error(), true)
				if !export && s != nil {
					s.Close()
				}
			} else {
				a.setStatus(msg, false)
				a.succeeded = export
				if !export {
					a.held = s
					a.scanned = src
					a.items = items
					a.picked = map[string]bool{}
				}
			}
			if historyErr != nil {
				a.setStatus(a.status+" · 本机记录："+historyErr.Error(), true)
			}
		})
	}()
}
func cli() error {
	f := flag.NewFlagSet(os.Args[1], flag.ContinueOnError)
	src := f.String("source", "", "外置 OpenAria 内存卡分区；省略时选择第一张")
	out := f.String("output", "", "输出目录")
	id := f.String("id", "", "session_id，逗号分隔；all 为全部有效条目")
	_ = f.Bool("playback", true, "兼容旧命令；现在默认导出合成视频")
	serial := f.Bool("serial-export", false, "只运行一条编码任务，跳过并发试测")
	_ = f.Bool("videotoolbox", true, "兼容旧命令；现在自动选择硬件编码")
	if e := f.Parse(os.Args[2:]); e != nil {
		return e
	}
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt)
	defer cancel()
	if *src == "" {
		r := discoverCards(ctx, true)
		if len(r.Cards) == 0 {
			return fmt.Errorf("%s", r.Message)
		}
		*src = r.Cards[0].Path
	}
	s, e := openSelectedCard(ctx, *src, "")
	if e != nil {
		return e
	}
	defer s.Close()
	vs, e := scanCard(ctx, s)
	if e != nil {
		return e
	}
	if os.Args[1] == "scan" {
		if err := loadLocalMarks(vs); err != nil {
			fmt.Fprintln(os.Stderr, "本机标记读取失败：", err)
		}
		return json.NewEncoder(os.Stdout).Encode(vs)
	}
	if *id == "" {
		return fmt.Errorf("export 需要 --id")
	}
	if *out == "" {
		*out = defaultOutputBase()
		if p, err := preferencesPath(); err == nil {
			if saved, err := loadPreferences(p); err == nil {
				*out = saved.OutputBase
			}
		}
	}
	*out = outputDirectory(*out)
	var selected []core.Session
	for _, v := range vs {
		if *id == "all" && v.Error == "" || strings.Contains(","+*id+",", ","+v.ID+",") {
			selected = append(selected, v)
		}
	}
	if len(selected) == 0 {
		return fmt.Errorf("没有匹配 session")
	}
	var historyErr error
	_, e = core.ExportBatch(ctx, s, selected, core.Options{Output: *out, Serial: *serial, VideoOnly: true}, func(i int, p core.Progress) {
		fmt.Fprintf(os.Stderr, "条目 %d/%d %s %s\n", i+1, len(selected), p.Stage, p.File)
	}, func(i int, p string) {
		fmt.Println(p)
		if _, err := saveLocalMark(selected[i], p); err != nil {
			historyErr = errors.Join(historyErr, fmt.Errorf("%s：视频已导出，但本机标记保存失败：%w", p, err))
		}
	})
	return errors.Join(e, historyErr)
}
func main() {
	if stop, err := ensureElevated(); err != nil {
		startupError(err)
		os.Exit(1)
	} else if stop {
		return
	}
	setupLogging()
	if runtime.GOOS == "darwin" {
		os.Setenv("PATH", os.Getenv("PATH")+":/opt/homebrew/bin:/usr/local/bin")
	}

	if len(os.Args) > 1 {
		if os.Args[1] == "source-helper" {
			if e := helper(os.Args[2:]); e != nil {
				log.Fatal(e)
			}
			return
		}
		if os.Args[1] == "scan" || os.Args[1] == "export" {
			if e := cli(); e != nil {
				log.Fatal(e)
			}
			return
		}
	}
	a := &app{picked: map[string]bool{}, out: defaultOutputBase(), status: "正在检测 OpenAria 内存卡…", cardMessage: "正在检测内存卡…"}
	if p, err := preferencesPath(); err == nil {
		if saved, err := loadPreferences(p); err == nil {
			a.out = saved.OutputBase
		} else {
			log.Printf("读取保存位置失败：%v", err)
		}
	}
	a.setupUpdates()
	mygo.App.WhenReady(func() {
		a.w = mygo.NewWindow(mygo.WindowOptions{Title: "Open Aria 录像导出", Width: 1120, Height: 760, MinWidth: 960, MinHeight: 640, TitleBarStyle: mygo.TitleBarHidden, StateKey: "main", Content: ui.View(a.view)})
		a.w.OnClose(a.beforeClose)
		a.startCardMonitor()
		a.firstDeviceAccessGuide()
	})
	if e := mygo.App.Run(); e != nil {
		log.Fatal(e)
	}
}

func (a *app) changeSource(p string) {
	if a.cancel != nil {
		a.cancel()
	}
	if a.held != nil {
		a.held.Close()
		a.held = nil
	}
	a.src = p
	a.scanned = ""
	a.items = nil
	a.picked = map[string]bool{}
}

func progressText(p core.Progress, entry, count int, base, total int64) string {
	prefix := fmt.Sprintf("条目 %d/%d · ", entry, count)
	if p.Stage == "合并编码" || p.Stage == "边读边编码" {
		return prefix + p.Stage + " · 输出时间 " + p.File
	}
	text := fmt.Sprintf("%s%s · %.1f/%.1f MiB · %s", prefix, p.Stage, float64(base+p.Bytes)/(1<<20), float64(total)/(1<<20), p.File)
	if p.Rate > 0 {
		text += fmt.Sprintf(" · %.1f MiB/s", p.Rate/(1<<20))
	}
	return text
}

func (a *app) setStatus(s string, failed bool) {
	a.status = s
	a.failed = failed
	a.succeeded = false
}

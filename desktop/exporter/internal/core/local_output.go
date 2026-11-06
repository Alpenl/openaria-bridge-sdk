package core

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"openaria-exporter/internal/source"
	"os"
	"path"
	"path/filepath"
	"reflect"
	"sort"
	"strings"
)

type LocalFile struct {
	Size      int64
	Modified  int64
	SHA       string
	Directory bool `json:",omitempty"`
}
type LocalOutput struct {
	Path  string
	Files map[string]LocalFile
}

func outputRoot(p string) (*os.Root, error) {
	if !filepath.IsAbs(p) || filepath.Clean(p) != p {
		return nil, fmt.Errorf("无效成品路径")
	}
	// Reject aliases in every component, including a replaced parent directory.
	for cur := p; ; cur = filepath.Dir(cur) {
		st, err := os.Lstat(cur)
		if err != nil {
			return nil, err
		}
		if !st.IsDir() || st.Mode()&os.ModeSymlink != 0 {
			return nil, fmt.Errorf("成品路径包含链接或非目录：%s", cur)
		}
		if filepath.Dir(cur) == cur {
			break
		}
	}
	return os.OpenRoot(p)
}
func inspectOutput(r *os.Root, p string) (LocalOutput, error) {
	out := LocalOutput{Path: p, Files: map[string]LocalFile{}}
	allowed, err := sourceOutputInventory(r)
	if err != nil {
		return out, err
	}
	f, err := r.Open(".")
	if err != nil {
		return out, err
	}
	entries, err := f.ReadDir(-1)
	f.Close()
	if err != nil {
		return out, err
	}
	for _, e := range entries {
		name := e.Name()
		if name == "source" && len(allowed) > 0 {
			if err := inspectSourceOutput(r, name, allowed, out.Files); err != nil {
				return out, err
			}
			continue
		}
		if name != "video.mp4" && name != "export.json" && name != ".claim" && name != ".lock" {
			return out, fmt.Errorf("成品目录有额外内容，未删除：%s", name)
		}
		st, err := r.Lstat(name)
		if err != nil {
			return out, err
		}
		if !st.Mode().IsRegular() {
			return out, fmt.Errorf("拒绝删除链接或特殊文件：%s", name)
		}
		item := LocalFile{Size: st.Size(), Modified: st.ModTime().UnixNano()}
		if name != "video.mp4" {
			if st.Size() > 16<<20 {
				return out, fmt.Errorf("成品控制文件过大")
			}
			f, err := r.Open(name)
			if err != nil {
				return out, err
			}
			b, err := io.ReadAll(io.LimitReader(f, 16<<20+1))
			f.Close()
			if err != nil {
				return out, err
			}
			h := sha256.Sum256(b)
			item.SHA = hex.EncodeToString(h[:])
		}
		out.Files[name] = item
	}
	if _, ok := out.Files["video.mp4"]; !ok {
		return out, fmt.Errorf("成品视频不存在")
	}
	if _, ok := out.Files["export.json"]; !ok {
		return out, fmt.Errorf("成品导出记录不存在")
	}
	for name, size := range allowed {
		if file, ok := out.Files[name]; !ok || file.Directory || file.Size != size {
			return out, fmt.Errorf("原始文件缺失或大小改变：%s", name)
		}
	}
	return out, nil
}

// The small generated receipt defines the exact file set. Never accept arbitrary
// new files under source, nor recursively delete a directory with RemoveAll.
func sourceOutputInventory(r *os.Root) (map[string]int64, error) {
	st, err := r.Lstat("export.json")
	if err != nil {
		return nil, err
	}
	if !st.Mode().IsRegular() || st.Size() > 16<<20 {
		return nil, fmt.Errorf("无效导出记录")
	}
	b, err := r.ReadFile("export.json")
	if err != nil {
		return nil, err
	}
	var receipt struct {
		Files map[string]int64 `json:"source_files"`
	}
	if err = json.Unmarshal(b, &receipt); err != nil {
		return nil, err
	}
	for name, size := range receipt.Files {
		if !strings.HasPrefix(name, "source/") || source.Safe(name) != nil || size < 0 {
			return nil, fmt.Errorf("无效原始文件清单")
		}
	}
	return receipt.Files, nil
}
func inspectSourceOutput(r *os.Root, name string, allowed map[string]int64, files map[string]LocalFile) error {
	st, err := r.Lstat(name)
	if err != nil {
		return err
	}
	if st.IsDir() {
		if strings.Count(name, "/") > 65 {
			return fmt.Errorf("原始目录过深")
		}
		files[name] = LocalFile{Directory: true}
		f, err := r.Open(name)
		if err != nil {
			return err
		}
		entries, err := f.ReadDir(-1)
		f.Close()
		if err != nil {
			return err
		}
		if len(entries) == 0 {
			return fmt.Errorf("额外空目录：%s", name)
		}
		for _, entry := range entries {
			if err = inspectSourceOutput(r, path.Join(name, entry.Name()), allowed, files); err != nil {
				return err
			}
		}
		return nil
	}
	if !st.Mode().IsRegular() {
		return fmt.Errorf("拒绝链接或特殊文件：%s", name)
	}
	size, ok := allowed[name]
	if !ok || size != st.Size() {
		return fmt.Errorf("原始文件已改变或有额外内容：%s", name)
	}
	item := LocalFile{Size: st.Size(), Modified: st.ModTime().UnixNano()}
	if name == "source/manifest.json" {
		if st.Size() > 8<<20 {
			return fmt.Errorf("原始清单过大")
		}
		b, err := r.ReadFile(name)
		if err != nil {
			return err
		}
		h := sha256.Sum256(b)
		item.SHA = hex.EncodeToString(h[:])
	}
	files[name] = item
	return nil
}
func InspectLocalOutput(p string, v Session) (LocalOutput, error) {
	p, err := filepath.Abs(p)
	if err != nil {
		return LocalOutput{}, err
	}
	// Persist the resolved path so macOS /var -> /private/var is not an alias later.
	p, err = filepath.EvalSymlinks(p)
	if err != nil {
		return LocalOutput{}, err
	}
	r, err := outputRoot(p)
	if err != nil {
		return LocalOutput{}, err
	}
	defer r.Close()
	out, err := inspectOutput(r, p)
	if err != nil {
		return out, err
	}
	b, err := r.ReadFile("export.json")
	if err != nil {
		return out, err
	}
	var receipt struct {
		Session  string `json:"session_id"`
		Manifest string `json:"source_manifest_sha256"`
		File     string `json:"file"`
		Bytes    int64  `json:"bytes"`
	}
	if err = json.Unmarshal(b, &receipt); err != nil {
		return out, err
	}
	h := sha256.Sum256(v.Raw)
	if receipt.Session != v.ID || receipt.Manifest != hex.EncodeToString(h[:]) || receipt.File != "video.mp4" || receipt.Bytes != out.Files["video.mp4"].Size {
		return out, fmt.Errorf("成品与录像身份不一致")
	}
	if original, ok := out.Files["source/manifest.json"]; ok && original.SHA != receipt.Manifest {
		return out, fmt.Errorf("原始清单与录像身份不一致")
	}
	return out, nil
}

// CheckLocalOutput is used before confirmation and again under the export lock.
func CheckLocalOutput(out LocalOutput) error {
	r, err := outputRoot(out.Path)
	if err != nil {
		return err
	}
	defer r.Close()
	current, err := inspectOutput(r, out.Path)
	if err != nil {
		return err
	}
	if !reflect.DeepEqual(current, out) {
		return fmt.Errorf("成品在导出后发生变化，未删除：%s", out.Path)
	}
	return nil
}

// DeleteLocalOutput removes only the exact known files, never recursively walks
// user content. It shares Export's stable parent lock; marks are intentionally kept.
func DeleteLocalOutput(out LocalOutput) error {
	parent, err := outputRoot(filepath.Dir(out.Path))
	if err != nil {
		return err
	}
	defer parent.Close()
	name := filepath.Base(out.Path)
	lockName := "." + name + ".lock"
	if err = targetSafe(parent, lockName); err != nil {
		return err
	}
	lock, err := parent.OpenFile(lockName, os.O_CREATE|os.O_RDWR, 0600)
	if err != nil {
		return err
	}
	defer lock.Close()
	if err = lockFile(lock); err != nil {
		return fmt.Errorf("此成品正在被其他任务使用：%w", err)
	}
	if err = CheckLocalOutput(out); err != nil {
		return err
	}
	before, err := parent.Lstat(name)
	if err != nil {
		return err
	}
	if !before.IsDir() || before.Mode()&os.ModeSymlink != 0 {
		return fmt.Errorf("成品目录已被替换")
	}
	r, err := parent.OpenRoot(name)
	if err != nil {
		return err
	}
	opened, err := r.Stat(".")
	if err != nil || !os.SameFile(before, opened) {
		r.Close()
		return fmt.Errorf("成品目录在打开时发生变化")
	}
	current, err := inspectOutput(r, out.Path)
	if err != nil || !reflect.DeepEqual(current, out) {
		r.Close()
		return fmt.Errorf("成品在删除前发生变化：%v", err)
	}
	var names []string
	for name := range out.Files {
		names = append(names, name)
	}
	// Children before their parents; the receipt last so interrupted deletion
	// cannot be mistaken for an intact export. Remove directories only if empty.
	sort.Slice(names, func(i, j int) bool {
		if names[i] == "export.json" {
			return false
		}
		if names[j] == "export.json" {
			return true
		}
		return names[i] > names[j]
	})
	for _, file := range names {
		if _, ok := out.Files[file]; !ok {
			continue
		}
		if err = r.Remove(file); err != nil {
			r.Close()
			return fmt.Errorf("成品删除中断（可能已删除部分文件）：%w", err)
		}
	}
	r.Close()
	// Remove only an empty directory. Any newly added file is preserved.
	return parent.Remove(name)
}

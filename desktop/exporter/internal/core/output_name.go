package core

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"openaria-exporter/internal/source"
	"os"
	"path/filepath"
	"strings"
)

const controlDirectory = ".openaria"

func recordingName(v Session, mode string, n int) string {
	t, ok := recordingDate(sessionTime(v).StartedAt)
	name := "录制时间未知"
	if ok {
		name = t.Format("2006-01-02 15时04分05秒")
	}
	if !ok || n > 1 {
		name += fmt.Sprintf("（%d）", n)
	}
	if mode == "raw" {
		name += "（未合成）"
	}
	return name
}
func digestName(s string) string { h := sha256.Sum256([]byte(s)); return hex.EncodeToString(h[:]) }

// Stable locks live outside renamed output folders and are never unlinked.
// Keep them together; Windows requires an explicit hidden attribute.
func controlRoot(root *os.Root) (*os.Root, error) {
	if err := targetSafe(root, controlDirectory); err != nil {
		return nil, err
	}
	if err := root.MkdirAll(controlDirectory, 0700); err != nil {
		return nil, err
	}
	if err := hideControlDirectory(filepath.Join(root.Name(), controlDirectory)); err != nil {
		return nil, err
	}
	return root.OpenRoot(controlDirectory)
}
func managedLock(root *os.Root, key string) (*os.File, error) {
	control, err := controlRoot(root)
	if err != nil {
		return nil, fmt.Errorf("准备导出锁目录：%w", err)
	}
	defer control.Close()
	name := digestName(key) + ".lock"
	if err = targetSafe(control, name); err != nil {
		return nil, err
	}
	// Separate creation from opening an existing lock. Concurrent O_CREATE
	// opens can return ENOENT on APFS; O_EXCL gives one unambiguous creator.
	f, err := control.OpenFile(name, os.O_CREATE|os.O_EXCL|os.O_RDWR, 0600)
	if os.IsExist(err) {
		f, err = control.OpenFile(name, os.O_RDWR, 0600)
	}
	if err != nil {
		return nil, fmt.Errorf("打开导出锁 %s：%w", key, err)
	}
	if err = lockFile(f); err != nil {
		f.Close()
		return nil, err
	}
	return f, nil
}
func readControl(root *os.Root, name string, limit int64) ([]byte, error) {
	if err := targetSafe(root, name); err != nil {
		return nil, err
	}
	st, err := root.Lstat(name)
	if err != nil {
		return nil, err
	}
	if !st.Mode().IsRegular() || st.Size() > limit {
		return nil, fmt.Errorf("无效控制文件：%s", name)
	}
	return root.ReadFile(name)
}
func matchingOutput(root *os.Root, name string, v Session, mode string) (bool, error) {
	st, err := root.Lstat(name)
	if err != nil {
		return false, err
	}
	if !st.IsDir() || st.Mode()&os.ModeSymlink != 0 {
		return false, nil
	}
	r, err := root.OpenRoot(name)
	if err != nil {
		return false, err
	}
	defer r.Close()
	h := sha256.Sum256(v.Raw)
	digest := hex.EncodeToString(h[:])
	if mode == "raw" {
		b, err := readControl(r, "receipt.json", 16<<20)
		if os.IsNotExist(err) {
			return false, nil
		}
		if err != nil {
			return false, err
		}
		var rec Receipt
		if json.Unmarshal(b, &rec) != nil {
			return false, nil
		}
		return rec.Session == v.ID && rec.ManifestSHA == digest && rec.Mode == mode, nil
	}
	b, err := readControl(r, "export.json", 16<<20)
	if os.IsNotExist(err) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	var rec struct {
		Session  string `json:"session_id"`
		Digest   string `json:"source_manifest_sha256"`
		Retained bool   `json:"source_copies_retained"`
	}
	if json.Unmarshal(b, &rec) != nil {
		return false, nil
	}
	// Old video-only products are preserved but do not count as a full-data export.
	return rec.Session == v.ID && rec.Digest == digest && rec.Retained, nil
}
func validateExistingOutput(root *os.Root, name string, v Session) error {
	out, err := InspectLocalOutput(filepath.Join(root.Name(), name), v)
	if err != nil {
		return err
	}
	for _, a := range v.Artifacts {
		f, ok := out.Files["source/"+a.Path]
		if !ok || f.Directory || f.Size != a.Bytes {
			return fmt.Errorf("已有导出缺少原始录制文件：%s", a.Path)
		}
	}
	if _, ok := out.Files["source/manifest.json"]; !ok {
		return fmt.Errorf("已有导出缺少原始清单")
	}
	return nil
}

// Called while the per-session lock is held. A per-name lock reserves the
// selected human name for the full export, including when another batch uses
// the same second. No existence-check/rename overwrite race is permitted.
func reserveRecordingName(ctx context.Context, root *os.Root, v Session, mode string) (name string, lock *os.File, existing bool, err error) {
	legacy := v.ID + "-" + mode
	if source.Safe(legacy) != nil {
		return "", nil, false, fmt.Errorf("无效录像标识")
	}
	// Reuse complete 0.1.9 output under the old name, without renaming it. Honor
	// its old lock so an older application cannot delete it during validation.
	if _, e := root.Lstat(legacy); e == nil {
		oldLock := "." + legacy + ".lock"
		if e = targetSafe(root, oldLock); e != nil {
			return "", nil, false, e
		}
		f, e := root.OpenFile(oldLock, os.O_CREATE|os.O_RDWR, 0600)
		if e != nil {
			return "", nil, false, e
		}
		if e = lockFile(f); e != nil {
			f.Close()
			return "", nil, false, fmt.Errorf("已有导出正在使用")
		}
		match, e := matchingOutput(root, legacy, v, mode)
		if e != nil {
			f.Close()
			return "", nil, false, e
		}
		if match {
			return legacy, f, true, nil
		}
		f.Close()
	} else if !os.IsNotExist(e) {
		return "", nil, false, e
	}
	// Search the whole name family before filling a gap left by a deleted output.
	// Otherwise deleting (1) would cause an existing (2) recording to be copied again.
	dir, e := root.Open(".")
	if e != nil {
		return "", nil, false, e
	}
	entries, e := dir.ReadDir(100001)
	dir.Close()
	if e != nil && e != io.EOF {
		return "", nil, false, e
	}
	if len(entries) > 100000 {
		return "", nil, false, fmt.Errorf("导出目录条目过多")
	}
	prefix := "录制时间未知"
	if t, ok := recordingDate(sessionTime(v).StartedAt); ok {
		prefix = t.Format("2006-01-02 15时04分05秒")
	}
	for _, entry := range entries {
		if !entry.IsDir() || !strings.HasPrefix(entry.Name(), prefix) {
			continue
		}
		if e := ctx.Err(); e != nil {
			return "", nil, false, e
		}
		match, e := matchingOutput(root, entry.Name(), v, mode)
		if e != nil {
			return "", nil, false, e
		}
		if !match {
			continue
		}
		f, e := managedLock(root, "output/"+entry.Name())
		if e != nil {
			return "", nil, false, fmt.Errorf("已有导出正在使用：%w", e)
		}
		match, e = matchingOutput(root, entry.Name(), v, mode)
		if e != nil {
			f.Close()
			return "", nil, false, e
		}
		if match {
			return entry.Name(), f, true, nil
		}
		f.Close()
	}
	for n := 1; n <= 10000; n++ {
		if e := ctx.Err(); e != nil {
			return "", nil, false, e
		}
		candidate := recordingName(v, mode, n)
		f, e := managedLock(root, "output/"+candidate)
		if e != nil {
			// A busy name is reserved by another recording. Distinguish lock contention
			// from permission/path failures so those never spin through 10,000 names.
			if lockBusy(e) {
				continue
			}
			return "", nil, false, e
		}
		if _, e = root.Lstat(candidate); os.IsNotExist(e) {
			return candidate, f, false, nil
		} else if e != nil {
			f.Close()
			return "", nil, false, e
		}
		match, e := matchingOutput(root, candidate, v, mode)
		if e != nil {
			f.Close()
			return "", nil, false, e
		}
		if match {
			return candidate, f, true, nil
		}
		f.Close()
	}
	return "", nil, false, fmt.Errorf("同名录像过多，请选择其他保存位置")
}

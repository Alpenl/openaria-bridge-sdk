package core

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"openaria-exporter/internal/sqliteutil"
	"os"
	"path"
	"path/filepath"
	"strings"

	_ "modernc.org/sqlite"
	"openaria-exporter/internal/source"
)

const catalogLimit int64 = 128 << 20

func catalogURI(filename string) string {
	return sqliteutil.FileURI(filename) + "?mode=rw"
}

// The original filesystem is never opened by SQLite. Only these bounded copies
// can be checkpointed/recovered; copying WAL is essential for committed rows.
func catalogCopy(ctx context.Context, s source.Source, p, dst string, optional bool) (string, error) {
	r, e := s.Open(p)
	if errors.Is(e, os.ErrNotExist) && optional {
		return "", nil
	}
	if e != nil {
		return "", e
	}
	defer r.Close()
	f, e := os.OpenFile(dst, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
	if e != nil {
		return "", e
	}
	h := sha256.New()
	n, e := copyContext(ctx, io.MultiWriter(f, h), io.LimitReader(r, catalogLimit+1), make([]byte, 1<<20), nil)
	ce := f.Close()
	if e != nil {
		return "", e
	}
	if ce != nil {
		return "", ce
	}
	if n > catalogLimit {
		return "", fmt.Errorf("catalog 文件超过 128MiB")
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}
func catalogDigest(ctx context.Context, s source.Source, p string) (string, error) {
	r, e := s.Open(p)
	if errors.Is(e, os.ErrNotExist) {
		return "", nil
	}
	if e != nil {
		return "", e
	}
	defer r.Close()
	h := sha256.New()
	n, e := copyContext(ctx, h, io.LimitReader(r, catalogLimit+1), make([]byte, 1<<20), nil)
	if n > catalogLimit {
		return "", fmt.Errorf("catalog 文件超过限制")
	}
	return hex.EncodeToString(h.Sum(nil)), e
}
func openCatalog(ctx context.Context, s source.Source, p string) (*sql.DB, func(), error) {
	d, e := os.MkdirTemp("", "openaria-catalog-")
	if e != nil {
		return nil, nil, e
	}
	cleanup := func() { os.RemoveAll(d) }
	hashes := map[string]string{}
	for _, suffix := range []string{"", "-wal", "-journal"} {
		h, e := catalogCopy(ctx, s, p+suffix, filepath.Join(d, "catalog.sqlite3"+suffix), suffix != "")
		if e != nil {
			cleanup()
			return nil, nil, e
		}
		hashes[suffix] = h
	}
	for _, suffix := range []string{"", "-wal", "-journal"} {
		h, e := catalogDigest(ctx, s, p+suffix)
		if e != nil || h != hashes[suffix] {
			cleanup()
			if e == nil {
				e = fmt.Errorf("读取期间 catalog 发生变化，请停止设备写入后重试")
			}
			return nil, nil, e
		}
	}
	db, e := sql.Open("sqlite", catalogURI(filepath.Join(d, "catalog.sqlite3")))
	if e != nil {
		cleanup()
		return nil, nil, e
	}
	db.SetMaxOpenConns(1)
	fail := func(e error) (*sql.DB, func(), error) { db.Close(); cleanup(); return nil, nil, e }
	if _, e = db.ExecContext(ctx, "PRAGMA trusted_schema=OFF"); e != nil {
		return fail(e)
	}
	var app, version int
	if e = db.QueryRowContext(ctx, "PRAGMA application_id").Scan(&app); e != nil {
		return fail(e)
	}
	if e = db.QueryRowContext(ctx, "PRAGMA user_version").Scan(&version); e != nil {
		return fail(e)
	}
	if app != 0x594c5801 || version != 1 {
		return fail(fmt.Errorf("不支持 catalog SQLite 身份/版本：%x/%d", app, version))
	}
	if _, e = db.ExecContext(ctx, "PRAGMA query_only=ON"); e != nil {
		return fail(e)
	}
	return db, func() { db.Close(); cleanup() }, nil
}

type catalogSummary struct {
	ID           string  `json:"session_id"`
	Name         string  `json:"display_name"`
	Started      string  `json:"started_at"`
	Duration     float64 `json:"duration_seconds"`
	TakeID       string  `json:"take_id"`
	TakeSequence int     `json:"take_sequence"`
}

func applyCatalog(ctx context.Context, db *sql.DB, items []Session, label string) error {
	// Require actual production tables/columns, rather than accepting an arbitrary
	// JSON-bearing database. Payloads and playback order still come from manifest.
	rows, e := db.QueryContext(ctx, "SELECT volume,session,signature,summary FROM sessions LIMIT 100001")
	if e != nil {
		return e
	}
	defer rows.Close()
	indexes := map[string]int{}
	for i, v := range items {
		if v.Manifest.VolumeID != "" {
			indexes[v.Manifest.VolumeID+"\x00"+v.ID] = i
		}
	}
	count := 0
	for rows.Next() {
		count++
		if count > 100000 {
			return fmt.Errorf("catalog 条目超过限制")
		}
		var volume, id, signature, raw string
		if e = rows.Scan(&volume, &id, &signature, &raw); e != nil {
			return e
		}
		i, ok := indexes[volume+"\x00"+id]
		if !ok {
			continue
		}
		v := &items[i]
		if len(raw) > 1<<20 || len(signature) > 8192 {
			v.MetadataWarning = "catalog 摘要超过限制，使用 manifest"
			continue
		}
		var sig []json.RawMessage
		var digest string
		if json.Unmarshal([]byte(signature), &sig) != nil || len(sig) == 0 || json.Unmarshal(sig[len(sig)-1], &digest) != nil {
			v.MetadataWarning = "catalog 签名无效，使用 manifest"
			continue
		}
		h := sha256.Sum256(v.Raw)
		if digest != hex.EncodeToString(h[:]) {
			v.MetadataWarning = "catalog 索引已过期，使用 manifest"
			continue
		}
		var summary catalogSummary
		if json.Unmarshal([]byte(raw), &summary) != nil || summary.ID != v.ID || strings.TrimSpace(summary.Name) == "" || len(summary.Name) > 640 || summary.Duration < 0 {
			v.MetadataWarning = "catalog 摘要无效，使用 manifest"
			continue
		}
		v.Name = summary.Name
		if summary.Started != "" {
			v.Manifest.Time.Started = summary.Started
		}
		v.Manifest.Time.Duration = summary.Duration
		v.TakeID = summary.TakeID
		v.TakeSequence = summary.TakeSequence
		v.MetadataSource = "sqlite:" + label
	}
	return rows.Err()
}
func enrichCatalog(ctx context.Context, s source.Source, items []Session) error {
	for _, root := range source.MetadataSources(s) {
		candidates := []string{"var/lib/rp-ylx/catalog.sqlite3", "catalog.sqlite3", "state/catalog.sqlite3"}
		if r, e := root.Open("etc/rp-ylx/device.json"); e == nil {
			b, e := io.ReadAll(io.LimitReader(r, (1<<20)+1))
			r.Close()
			var cfg struct {
				StateRoot string `json:"state_root"`
			}
			if e == nil && len(b) <= 1<<20 && json.Unmarshal(b, &cfg) == nil && strings.HasPrefix(cfg.StateRoot, "/") {
				p := path.Join(strings.TrimPrefix(cfg.StateRoot, "/"), "catalog.sqlite3")
				if source.Safe(p) == nil {
					candidates = append([]string{p}, candidates...)
				}
			}
		}
		seen := map[string]bool{}
		for _, p := range candidates {
			if seen[p] {
				continue
			}
			seen[p] = true
			db, closeFn, e := openCatalog(ctx, root, p)
			if errors.Is(e, os.ErrNotExist) {
				continue
			}
			if e != nil {
				return fmt.Errorf("读取录像 SQLite %s: %w", p, e)
			}
			e = applyCatalog(ctx, db, items, p)
			closeFn()
			if e != nil {
				return fmt.Errorf("读取录像 SQLite %s: %w", p, e)
			}
			n := 0
			for _, v := range items {
				if strings.HasPrefix(v.MetadataSource, "sqlite:") {
					n++
				}
			}
			log.Printf("catalog path=%q matched=%d sessions=%d", p, n, len(items))
			return nil
		}
	}
	return ctx.Err()
}

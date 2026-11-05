// Package history stores this client's export marks and output receipts.
package history

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"time"

	_ "modernc.org/sqlite"
	"openaria-exporter/internal/core"
	"openaria-exporter/internal/sqliteutil"
)

type Store struct{ Path string }
type Mark struct {
	ExportedAt string
	Output     string
}
type Output struct {
	ID      int64
	Path    string
	Receipt core.LocalOutput
}

func DefaultPath() (string, error) {
	var base string
	var err error
	if runtime.GOOS == "windows" {
		base, err = os.UserCacheDir()
	} else {
		base, err = os.UserConfigDir()
	}
	if err != nil {
		return "", err
	}
	return filepath.Join(base, "OpenAriaExporter", "history.sqlite3"), nil
}

// Revision depends on media descriptors, not names, drive letters or catalog labels.
// It hashes only small manifest data, never source media.
func Revision(v core.Session) string {
	b, _ := json.Marshal(struct {
		Video any
		Audio any
	}{v.Manifest.Video, v.Manifest.Audio})
	h := sha256.Sum256(b)
	return hex.EncodeToString(h[:])
}
func (s Store) open() (*sql.DB, error) {
	if s.Path == "" {
		return nil, fmt.Errorf("本机记录路径为空")
	}
	if err := os.MkdirAll(filepath.Dir(s.Path), 0700); err != nil {
		return nil, err
	}
	db, err := sql.Open("sqlite", sqliteutil.FileURI(s.Path)+"?_pragma=busy_timeout(5000)&_txlock=immediate")
	if err != nil {
		return nil, err
	}
	db.SetMaxOpenConns(1)
	// Acquire the writer reservation before reading or changing the schema.
	// Concurrent first opens must not try to upgrade separate schema read locks
	// while another client's writer is waiting to commit (SQLITE_BUSY).
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	tx, err := db.BeginTx(ctx, nil)
	if err != nil {
		db.Close()
		return nil, fmt.Errorf("开始本机记录初始化事务：%w", err)
	}
	_, err = tx.ExecContext(ctx, `CREATE TABLE IF NOT EXISTS marks(volume TEXT NOT NULL,session TEXT NOT NULL,revision TEXT NOT NULL,exported_at TEXT NOT NULL,output TEXT NOT NULL,PRIMARY KEY(volume,session,revision));
 CREATE TABLE IF NOT EXISTS outputs(id INTEGER PRIMARY KEY,volume TEXT NOT NULL,session TEXT NOT NULL,path TEXT NOT NULL UNIQUE,receipt TEXT NOT NULL);`)
	if err == nil {
		err = tx.Commit()
	} else {
		tx.Rollback()
	}
	if err != nil {
		db.Close()
		return nil, fmt.Errorf("初始化本机记录表：%w", err)
	}
	return db, nil
}
func (s Store) Record(v core.Session, dst string) (Mark, error) {
	receipt, err := core.InspectLocalOutput(dst, v)
	if err != nil {
		return Mark{}, err
	}
	payload, err := json.Marshal(receipt)
	if err != nil {
		return Mark{}, err
	}
	db, err := s.open()
	if err != nil {
		return Mark{}, err
	}
	defer db.Close()
	// A published output must be recorded even if the export batch was cancelled.
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	tx, err := db.BeginTx(ctx, nil)
	if err != nil {
		return Mark{}, err
	}
	defer tx.Rollback()
	m := Mark{time.Now().UTC().Format(time.RFC3339Nano), receipt.Path}
	_, err = tx.ExecContext(ctx, `INSERT INTO outputs(volume,session,path,receipt) VALUES(?,?,?,?) ON CONFLICT(path) DO UPDATE SET volume=excluded.volume,session=excluded.session,receipt=excluded.receipt`, v.Manifest.VolumeID, v.ID, m.Output, string(payload))
	if err == nil {
		_, err = tx.ExecContext(ctx, `INSERT INTO marks VALUES(?,?,?,?,?) ON CONFLICT(volume,session,revision) DO UPDATE SET exported_at=excluded.exported_at,output=excluded.output`, v.Manifest.VolumeID, v.ID, Revision(v), m.ExportedAt, m.Output)
	}
	if err != nil {
		return Mark{}, err
	}
	return m, tx.Commit()
}
func (s Store) Apply(items []core.Session) error {
	for i := range items {
		items[i].LocalExportedAt = ""
		items[i].LocalExportPath = ""
	}
	if _, err := os.Stat(s.Path); os.IsNotExist(err) {
		return nil
	}
	db, err := s.open()
	if err != nil {
		return err
	}
	defer db.Close()
	for i, v := range items {
		var m Mark
		err = db.QueryRow(`SELECT exported_at,output FROM marks WHERE volume=? AND session=? AND revision=?`, v.Manifest.VolumeID, v.ID, Revision(v)).Scan(&m.ExportedAt, &m.Output)
		if err == sql.ErrNoRows {
			continue
		}
		if err != nil {
			return err
		}
		items[i].LocalExportedAt = m.ExportedAt
		items[i].LocalExportPath = m.Output
	}
	return nil
}

// Clear affects marks only. Output receipts remain available for explicit deletion.
func (s Store) Clear(items []core.Session) error {
	db, err := s.open()
	if err != nil {
		return err
	}
	defer db.Close()
	tx, err := db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	for _, v := range items {
		if _, err = tx.Exec(`DELETE FROM marks WHERE volume=? AND session=?`, v.Manifest.VolumeID, v.ID); err != nil {
			return err
		}
	}
	return tx.Commit()
}
func (s Store) Outputs(items []core.Session) ([]Output, error) {
	db, err := s.open()
	if err != nil {
		return nil, err
	}
	defer db.Close()
	var out []Output
	for _, v := range items {
		rows, err := db.Query(`SELECT id,path,receipt FROM outputs WHERE volume=? AND session=? ORDER BY path`, v.Manifest.VolumeID, v.ID)
		if err != nil {
			return nil, err
		}
		for rows.Next() {
			var o Output
			var b string
			if err = rows.Scan(&o.ID, &o.Path, &b); err != nil {
				break
			}
			if err = json.Unmarshal([]byte(b), &o.Receipt); err != nil {
				break
			}
			if o.Path != o.Receipt.Path {
				err = fmt.Errorf("成品记录路径不一致")
				break
			}
			out = append(out, o)
		}
		re := rows.Err()
		rows.Close()
		if err != nil {
			return nil, err
		}
		if re != nil {
			return nil, re
		}
	}
	return out, nil
}
func (s Store) ForgetOutput(id int64) error {
	db, err := s.open()
	if err != nil {
		return err
	}
	defer db.Close()
	_, err = db.Exec(`DELETE FROM outputs WHERE id=?`, id)
	return err
}

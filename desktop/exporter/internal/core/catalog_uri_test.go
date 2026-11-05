package core

import (
	"database/sql"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestCatalogURIWindowsDrive(t *testing.T) {
	for _, path := range []string{"C:/Users/Test/AppData/Local/Temp/catalog.sqlite3", "D:/用户/Temp #1%/catalog.sqlite3"} {
		raw := catalogURI(path)
		u, err := url.Parse(raw)
		if err != nil {
			t.Fatal(err)
		}
		if u.Host != "" || u.Path != "/"+path || u.Query().Get("mode") != "rw" || !strings.HasPrefix(raw, "file:///") {
			t.Fatalf("invalid SQLite drive URI %q: %+v", raw, u)
		}
	}
}

func TestCatalogURIEscapedPath(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "用户 #1%")
	if err := os.Mkdir(dir, 0700); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(dir, "catalog.sqlite3")
	db, err := sql.Open("sqlite", path)
	if err != nil {
		t.Fatal(err)
	}
	if _, err = db.Exec("CREATE TABLE marker(value TEXT); INSERT INTO marker VALUES('correct file')"); err != nil {
		db.Close()
		t.Fatal(err)
	}
	if err = db.Close(); err != nil {
		t.Fatal(err)
	}
	db, err = sql.Open("sqlite", catalogURI(path))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	var value string
	if err = db.QueryRow("SELECT value FROM marker").Scan(&value); err != nil {
		t.Fatal(err)
	}
	if value != "correct file" {
		t.Fatal(value)
	}
}

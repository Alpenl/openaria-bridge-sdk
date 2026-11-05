// Package sqliteutil constructs file URIs accepted by SQLite on every platform.
package sqliteutil

import (
	"net/url"
	"path/filepath"
	"strings"
)

func FileURI(filename string) string {
	p := filepath.ToSlash(filename)
	// The leading slash prevents a Windows drive letter becoming an authority.
	if !strings.HasPrefix(p, "/") {
		p = "/" + p
	}
	return (&url.URL{Scheme: "file", Path: p}).String()
}

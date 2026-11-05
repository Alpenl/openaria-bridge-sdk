package main

import (
	"archive/tar"
	"compress/gzip"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"io"
	"os"
	"path/filepath"
)

func main() {
	dir := os.Args[1]
	pub, priv, _ := ed25519.GenerateKey(rand.Reader)
	os.WriteFile(filepath.Join(dir, "public"), []byte(base64.StdEncoding.EncodeToString(pub)), 0600)
	os.WriteFile(filepath.Join(dir, "private"), []byte(base64.StdEncoding.EncodeToString(priv)), 0600)
	if len(os.Args) < 3 {
		return
	}
	// A newly generated pair is used for each fixture; never printed.
	f, _ := os.Create(filepath.Join(dir, "update.tar.gz"))
	gz := gzip.NewWriter(f)
	tw := tar.NewWriter(gz)
	root := os.Args[2]
	err := filepath.Walk(root, func(p string, i os.FileInfo, e error) error {
		if e != nil {
			return e
		}
		h, e := tar.FileInfoHeader(i, "")
		if e != nil {
			return e
		}
		rel, _ := filepath.Rel(filepath.Dir(root), p)
		h.Name = filepath.ToSlash(rel)
		if e = tw.WriteHeader(h); e != nil {
			return e
		}
		if i.Mode().IsRegular() {
			r, e := os.Open(p)
			if e != nil {
				return e
			}
			defer r.Close()
			_, e = io.Copy(tw, r)
			return e
		}
		return nil
	})
	if err != nil {
		panic(err)
	}
	tw.Close()
	gz.Close()
	f.Close()
	b, _ := os.ReadFile(filepath.Join(dir, "update.tar.gz"))
	sum := sha256.Sum256(b)
	m := map[string]any{"version": "0.1.1", "url": "fixture", "size": len(b), "signature": base64.StdEncoding.EncodeToString(ed25519.Sign(priv, sum[:]))}
	data, _ := json.Marshal(m)
	os.WriteFile(filepath.Join(dir, "manifest.json"), data, 0600)
}

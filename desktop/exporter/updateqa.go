//go:build updateqa

package main

import (
	"context"
	"fmt"
	"github.com/egoist/mygo"
	"io"
	"os"
	"path/filepath"
)

// This entry point exists only in explicit QA builds. It cannot touch any path
// other than the fixture supplied by the integration harness.
type qaSource struct{ dir string }

func (s qaSource) Manifest(context.Context, bool) (io.ReadCloser, error) {
	return os.Open(filepath.Join(s.dir, "manifest.json"))
}
func (s qaSource) Open(_ context.Context, _ string) (io.ReadCloser, error) {
	return os.Open(filepath.Join(s.dir, "update.tar.gz"))
}
func init() {
	if len(os.Args) != 3 || os.Args[1] != "update-qa" {
		return
	}
	if mygo.App.Version() == "0.1.1" {
		fmt.Println("RESTARTED 0.1.1")
		os.Exit(0)
	}
	mygo.Updater.SetSource(qaSource{os.Args[2]})
	u, e := mygo.Updater.Check(context.Background())
	if e != nil {
		fmt.Fprintln(os.Stderr, e)
		os.Exit(2)
	}
	if u == nil {
		fmt.Fprintln(os.Stderr, "missing update")
		os.Exit(2)
	}
	if e = u.Install(context.Background(), nil); e != nil {
		fmt.Fprintln(os.Stderr, e)
		os.Exit(3)
	}
	fmt.Println("STAGED", u.Version)
	os.Exit(0)
}

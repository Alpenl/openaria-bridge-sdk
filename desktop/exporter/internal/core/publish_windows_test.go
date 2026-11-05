package core

import (
	"os"
	"testing"
)

func TestWindowsPublishWithParentLock(t *testing.T) {
	root, e := os.OpenRoot(t.TempDir())
	if e != nil {
		t.Fatal(e)
	}
	defer root.Close()
	lock, e := root.OpenFile(".final.lock", os.O_CREATE|os.O_RDWR, 0600)
	if e != nil {
		t.Fatal(e)
	}
	defer lock.Close()
	if e = lockFile(lock); e != nil {
		t.Fatal(e)
	}
	if e = root.Mkdir("stage", 0700); e != nil {
		t.Fatal(e)
	}
	st, e := root.OpenRoot("stage")
	if e != nil {
		t.Fatal(e)
	}
	defer st.Close()
	if e = writeNew(st, "video.mp4", []byte("finished")); e != nil {
		t.Fatal(e)
	}
	if e = publish(root, "stage", "final"); e != nil {
		t.Fatal(e)
	}
	contender, e := root.OpenFile(".final.lock", os.O_RDWR, 0600)
	if e != nil {
		t.Fatal(e)
	}
	defer contender.Close()
	if e = lockFile(contender); e == nil {
		t.Fatal("publication released the parent lock")
	}
	if e = root.Mkdir("other", 0700); e != nil {
		t.Fatal(e)
	}
	if e = publish(root, "other", "final"); e == nil {
		t.Fatal("overwrote existing destination")
	}
	if e = root.Mkdir("empty", 0700); e != nil {
		t.Fatal(e)
	}
	if e = publish(root, "other", "empty"); e == nil {
		t.Fatal("overwrote empty destination")
	}
}

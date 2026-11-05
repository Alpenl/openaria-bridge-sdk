package main

/*
#include <launch.h>
#include <stdlib.h>
static int activation(int *fd) {
 int *fds = NULL; size_t n = 0;
 int rc = launch_activate_socket("DeviceSocket", &fds, &n);
 if (rc) return rc;
 if (n != 1) { free(fds); return 22; }
 *fd = fds[0]; free(fds); return 0;
}
*/
import "C"
import (
	"context"
	"fmt"
	"log"
	"net"
	"openaria-exporter/internal/diagnostics"
	"openaria-exporter/internal/privileged"
	"os"
	"syscall"
	"time"
)

func main() {
	if e := run(); e != nil {
		log.Print(e)
		os.Exit(1)
	}
}
func run() error {
	if os.Geteuid() != 0 {
		return fmt.Errorf("root required")
	}
	uid, e := privileged.ReadUID()
	if e != nil {
		return e
	}
	if len(os.Args) == 2 && os.Args[1] == "--check" {
		if e = syscall.Setgroups([]int{}); e != nil {
			return e
		}
		if e = syscall.Setgid(int(uid)); e != nil {
			return e
		}
		if e = syscall.Setuid(int(uid)); e != nil {
			return e
		}
		var err error
		for i := 0; i < 20; i++ {
			_, err = privileged.RequestFile(context.Background(), "status", "")
			if err == nil {
				return nil
			}
			time.Sleep(250 * time.Millisecond)
		}
		return err
	}
	if len(os.Args) != 1 {
		return fmt.Errorf("invalid arguments")
	}
	w, e := diagnostics.New("/Library/Logs/OpenAriaExporter/service.log")
	if e != nil {
		return e
	}
	log.SetOutput(w)
	var fd C.int
	if code := C.activation(&fd); code != 0 {
		return fmt.Errorf("launch socket activation: %d", code)
	}
	f := os.NewFile(uintptr(fd), "launchd socket")
	l, e := net.FileListener(f)
	f.Close()
	if e != nil {
		return e
	}
	defer l.Close()
	return privileged.Serve(l.(*net.UnixListener), uid)
}

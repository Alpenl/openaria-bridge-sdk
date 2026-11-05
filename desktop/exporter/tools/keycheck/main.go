// Validate keys without printing private material.
package main

import (
	"bytes"
	"crypto/ed25519"
	"encoding/base64"
	"fmt"
	"os"
	"strings"
)

func main() {
	pub, e := os.ReadFile(os.Args[1])
	if e != nil {
		panic("cannot read public key")
	}
	p, e := base64.StdEncoding.DecodeString(strings.TrimSpace(string(pub)))
	if e != nil || len(p) != 32 {
		panic("invalid public key")
	}
	k, e := base64.StdEncoding.DecodeString(strings.TrimSpace(os.Getenv("MYGO_UPDATER_PRIVATE_KEY")))
	if e != nil || len(k) != 64 {
		panic("invalid private key")
	}
	derived := ed25519.NewKeyFromSeed(k[:32])
	if !bytes.Equal(k, derived) || !bytes.Equal(p, derived[32:]) {
		panic("key pair mismatch")
	}
	fmt.Println("Update key pair validated")
}

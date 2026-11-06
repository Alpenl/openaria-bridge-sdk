package main

import (
	"encoding/binary"
	"reflect"
	"testing"
)

func TestNativePhysicalDiskNumbers(t *testing.T) {
	got := physicalDiskNumbers([]string{"C:", "PhysicalDrive0", "PhysicalDrive9", "PhysicalDrive2", "PhysicalDrive2", "PhysicalDrive-1", "PhysicalDrive01", "PhysicalDrive4294967296", "PhysicalDrive1suffix"})
	if !reflect.DeepEqual(got, []uint32{0, 2, 9}) {
		t.Fatal(got)
	}
}
func TestNativeExternalBusAllowlist(t *testing.T) {
	for bus := uint32(0); bus < 30; bus++ {
		want := bus == 7 || bus == 12 || bus == 13
		if externalStorageBus(bus) != want {
			t.Fatal(bus)
		}
		b := make([]byte, 36)
		binary.LittleEndian.PutUint32(b[4:], 36)
		binary.LittleEndian.PutUint32(b[28:], bus)
		got, e := storageBus(b)
		if e != nil || got != bus {
			t.Fatal(got, e)
		}
	}
	for _, b := range [][]byte{nil, make([]byte, 35), make([]byte, 36)} {
		if _, e := storageBus(b); e == nil {
			t.Fatal("invalid descriptor accepted")
		}
	}
}
func layoutFixture(style uint32) []byte {
	b := make([]byte, 48+2*144)
	binary.LittleEndian.PutUint32(b, style)
	binary.LittleEndian.PutUint32(b[4:], 2)
	for i := 0; i < 2; i++ {
		e := b[48+i*144:]
		binary.LittleEndian.PutUint32(e, style)
		binary.LittleEndian.PutUint64(e[8:], uint64(1048576+i*268435456))
		binary.LittleEndian.PutUint64(e[16:], 268435456)
		binary.LittleEndian.PutUint32(e[24:], uint32(i+1))
	}
	return b
}
func TestNativeMBRAndGPTLayout(t *testing.T) {
	for _, style := range []uint32{0, 1} {
		parts, e := nativePartitions(layoutFixture(style), 3)
		if e != nil || len(parts) != 2 {
			t.Fatal(parts, e)
		}
		if parts[1].Disk != 3 || parts[1].Partition != 2 || parts[1].Offset != 269484032 || parts[1].Size != 268435456 {
			t.Fatal(parts)
		}
	}
}
func TestNativeLayoutRejectsMalformedBounds(t *testing.T) {
	cases := map[string]func([]byte) []byte{
		"short":     func(b []byte) []byte { return b[:47] },
		"truncated": func(b []byte) []byte { return b[:len(b)-1] },
		"count":     func(b []byte) []byte { binary.LittleEndian.PutUint32(b[4:], 0xffffffff); return b },
		"negative":  func(b []byte) []byte { binary.LittleEndian.PutUint64(b[56:], 1<<63); return b },
		"overflow":  func(b []byte) []byte { binary.LittleEndian.PutUint64(b[64:], 1<<63-1); return b },
		"duplicate": func(b []byte) []byte { binary.LittleEndian.PutUint32(b[48+144+24:], 1); return b },
		"style":     func(b []byte) []byte { binary.LittleEndian.PutUint32(b[48:], 1); return b },
	}
	for name, mutate := range cases {
		t.Run(name, func(t *testing.T) {
			if _, e := nativePartitions(mutate(layoutFixture(0)), 1); e == nil {
				t.Fatal("invalid layout accepted")
			}
		})
	}
	b := make([]byte, 48)
	binary.LittleEndian.PutUint32(b, 2)
	if p, e := nativePartitions(b, 1); e != nil || len(p) != 0 {
		t.Fatal(p, e)
	}
}

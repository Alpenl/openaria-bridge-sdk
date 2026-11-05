package main

import "testing"

func TestExternalDiskInventory(t *testing.T) {
	r, e := parseExternalDisks([]byte(`{"AllDisksAndPartitions":[{"DeviceIdentifier":"disk5","Size":68719476736,"Partitions":[{"DeviceIdentifier":"disk5s1","Content":"EFI"},{"DeviceIdentifier":"disk5s2","Content":"Linux"}]},{"DeviceIdentifier":"disk6","Size":32000000000,"Partitions":[{"DeviceIdentifier":"disk6s1","Content":"Microsoft Basic Data"}]}]}`))
	if e != nil || r.External != 2 || len(r.Candidates) != 1 || r.Candidates[0].Path != "/dev/disk5s2" {
		t.Fatalf("%+v %v", r, e)
	}
	r, e = externalPartitions(t.Context())
	if e != nil {
		t.Fatalf("real diskutil enumeration: %v", e)
	}
	t.Logf("physical external inventory: %+v", r)
}

package main

import (
	"encoding/binary"
	"fmt"
	"math"
	"sort"
	"strconv"
	"strings"
)

// Native Windows structures are decoded by documented byte offsets, not Go
// struct packing. DRIVE_LAYOUT_INFORMATION_EX has a 48-byte header and
// PARTITION_INFORMATION_EX entries are 144 bytes on supported Windows targets.
func physicalDiskNumbers(names []string) []uint32 {
	seen := map[uint32]bool{}
	for _, name := range names {
		if !strings.HasPrefix(name, "PhysicalDrive") {
			continue
		}
		text := strings.TrimPrefix(name, "PhysicalDrive")
		n, err := strconv.ParseUint(text, 10, 32)
		if err == nil && strconv.FormatUint(n, 10) == text {
			seen[uint32(n)] = true
		}
	}
	result := make([]uint32, 0, len(seen))
	for n := range seen {
		result = append(result, n)
	}
	sort.Slice(result, func(i, j int) bool { return result[i] < result[j] })
	return result
}

func storageBus(b []byte) (uint32, error) {
	if len(b) < 36 {
		return 0, fmt.Errorf("存储设备描述符不足：%d 字节", len(b))
	}
	if binary.LittleEndian.Uint32(b[4:8]) < 36 {
		return 0, fmt.Errorf("存储设备描述符长度无效")
	}
	return binary.LittleEndian.Uint32(b[28:32]), nil
}
func externalStorageBus(bus uint32) bool { return bus == 7 || bus == 12 || bus == 13 } // USB, SD, MMC only

func nativePartitions(b []byte, disk uint32) ([]winPartition, error) {
	if len(b) < 48 {
		return nil, fmt.Errorf("分区布局头不足：%d 字节", len(b))
	}
	style := binary.LittleEndian.Uint32(b[:4])
	count := binary.LittleEndian.Uint32(b[4:8])
	if style == 2 && count == 0 {
		return nil, nil
	} // unpartitioned RAW device
	if style > 1 {
		return nil, fmt.Errorf("不支持的分区布局：%d", style)
	}
	if uint64(count) > uint64((len(b)-48)/144) {
		return nil, fmt.Errorf("分区布局被截断：%d 个分区，%d 字节", count, len(b))
	}
	var out []winPartition
	seen := map[uint32]bool{}
	for i := uint32(0); i < count; i++ {
		entry := b[48+int(i)*144 : 48+int(i+1)*144]
		number := binary.LittleEndian.Uint32(entry[24:28])
		length := binary.LittleEndian.Uint64(entry[16:24])
		if number == 0 || length == 0 {
			continue
		} // unused MBR table entries
		offset := binary.LittleEndian.Uint64(entry[8:16])
		if binary.LittleEndian.Uint32(entry[:4]) != style || offset == 0 || offset > math.MaxInt64 || length > math.MaxInt64-offset || seen[number] {
			return nil, fmt.Errorf("磁盘 %d 分区 %d 的布局无效", disk, number)
		}
		seen[number] = true
		out = append(out, winPartition{Disk: disk, Partition: number, Offset: int64(offset), Size: int64(length), Name: fmt.Sprintf("外置磁盘 %d", disk)})
	}
	return out, nil
}

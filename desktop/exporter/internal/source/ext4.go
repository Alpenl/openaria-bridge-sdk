package source

import (
	"encoding/binary"
	"fmt"
	"io"
	"io/fs"
	"math"
	"sort"
	"strings"
)

var le = binary.LittleEndian

type Ext4 struct {
	r                   io.ReaderAt
	bs, ipg, is, desc   uint64
	groups              uint64
	close               func() error
	JournalPending      bool
	OuterJournalPending bool
	outer               *Ext4
	cache               map[uint32]*inode
	dirs                map[*inode]map[string]uint32
}
type extent struct {
	logical, physical, length uint64
	zero                      bool
}
type inode struct {
	x     *Ext4
	size  int64
	mode  uint16
	ext   []extent
	nodes int
}

func read(r io.ReaderAt, off uint64, n int) ([]byte, error) {
	if off > math.MaxInt64-uint64(n) {
		return nil, fmt.Errorf("offset overflow")
	}
	b := make([]byte, n)
	_, e := r.ReadAt(b, int64(off))
	return b, e
}
func NewExt4(r io.ReaderAt) (*Ext4, error) {
	b, e := read(r, 1024, 1024)
	if e != nil {
		return nil, e
	}
	if le.Uint16(b[56:]) != 0xef53 {
		return nil, fmt.Errorf("不是 ext4 分区镜像（不支持整个磁盘分区表）")
	}
	log := le.Uint32(b[24:])
	if log > 6 {
		return nil, fmt.Errorf("ext4 block size")
	}
	flags := le.Uint32(b[96:])
	if flags & ^uint32(0x2|0x4|0x40|0x80|0x200|0x2000) != 0 {
		return nil, fmt.Errorf("不支持 ext4 incompat flags %#x", flags)
	}
	x := &Ext4{r: r, bs: 1024 << log, ipg: uint64(le.Uint32(b[40:])), is: uint64(le.Uint16(b[88:])), desc: 32, JournalPending: flags&4 != 0}
	if flags&0x80 != 0 {
		x.desc = uint64(le.Uint16(b[254:]))
	}
	if x.ipg == 0 || x.is < 128 || x.is > x.bs || x.desc < 32 || x.desc > x.bs {
		return nil, fmt.Errorf("损坏的 ext4 参数")
	}
	x.groups = (uint64(le.Uint32(b)) + x.ipg - 1) / x.ipg
	return x, nil
}
func (x *Ext4) get(num uint32) (*inode, error) {
	if n := x.cache[num]; n != nil {
		return n, nil
	}
	if num == 0 {
		return nil, fmt.Errorf("invalid inode")
	}
	g := uint64(num-1) / x.ipg
	if g >= x.groups {
		return nil, fmt.Errorf("inode out of range")
	}
	base := x.bs
	if x.bs == 1024 {
		base = 2048
	}
	d, e := read(x.r, base+g*x.desc, int(x.desc))
	if e != nil {
		return nil, e
	}
	table := uint64(le.Uint32(d[8:]))
	if x.desc >= 64 {
		table |= uint64(le.Uint32(d[40:])) << 32
	}
	if table > math.MaxInt64/x.bs {
		return nil, fmt.Errorf("inode table overflow")
	}
	b, e := read(x.r, table*x.bs+(uint64(num-1)%x.ipg)*x.is, int(x.is))
	if e != nil {
		return nil, e
	}
	n := &inode{x: x, mode: le.Uint16(b), size: int64(uint64(le.Uint32(b[4:])) | uint64(le.Uint32(b[108:]))<<32)}
	if table > math.MaxInt64/x.bs {
		return nil, fmt.Errorf("inode table overflow")
	}
	if n.size < 0 {
		return nil, fmt.Errorf("size overflow")
	}
	if n.mode&0xf000 == 0xa000 {
		return nil, fmt.Errorf("拒绝 ext4 符号链接")
	}
	if le.Uint32(b[32:])&0x80000 == 0 {
		return nil, fmt.Errorf("仅支持 extent inode")
	}
	e = n.walk(b[40:100], 0)
	if e == nil {
		sort.Slice(n.ext, func(i, j int) bool { return n.ext[i].logical < n.ext[j].logical })
		for i, v := range n.ext {
			if v.length == 0 || v.physical > math.MaxInt64/x.bs-v.length || v.logical > math.MaxInt64/x.bs-v.length {
				return nil, fmt.Errorf("extent overflow")
			}
			if i > 0 && n.ext[i-1].logical+n.ext[i-1].length > v.logical {
				return nil, fmt.Errorf("overlapping extents")
			}
		}
		if len(x.cache) >= 256 {
			x.cache = nil
			x.dirs = nil
		}
		if x.cache == nil {
			x.cache = map[uint32]*inode{}
		}
		x.cache[num] = n
	}
	return n, e
}
func (n *inode) walk(b []byte, level int) error {
	n.nodes++
	if n.nodes > 65536 {
		return fmt.Errorf("extent tree node limit")
	}
	if len(b) < 12 || le.Uint16(b) != 0xf30a || level > 5 {
		return fmt.Errorf("损坏 extent tree")
	}
	count := int(le.Uint16(b[2:]))
	depth := le.Uint16(b[6:])
	if 12+12*count > len(b) || depth > 5 || len(n.ext)+count > 65536 {
		return fmt.Errorf("extent bounds")
	}
	for i := 0; i < count; i++ {
		v := b[12+i*12:]
		if depth == 0 {
			l := uint64(le.Uint16(v[4:]))
			zero := l > 32768
			if zero {
				l -= 32768
			}
			n.ext = append(n.ext, extent{uint64(le.Uint32(v)), uint64(le.Uint16(v[6:]))<<32 | uint64(le.Uint32(v[8:])), l, zero})
		} else {
			block := (uint64(le.Uint16(v[8:]))<<32 | uint64(le.Uint32(v[4:])))
			if block > math.MaxInt64/n.x.bs {
				return fmt.Errorf("extent index overflow")
			}
			off := block * n.x.bs
			child, e := read(n.x.r, off, int(n.x.bs))
			if e != nil {
				return e
			}
			if le.Uint16(child[6:])+1 != depth {
				return fmt.Errorf("extent depth")
			}
			if e = n.walk(child, level+1); e != nil {
				return e
			}
		}
	}
	return nil
}
func (n *inode) ReadAt(p []byte, off int64) (int, error) {
	if off < 0 {
		return 0, fmt.Errorf("negative offset")
	}
	if off >= n.size {
		return 0, io.EOF
	}
	want := len(p)
	if int64(want) > n.size-off {
		want = int(n.size - off)
	}
	done := 0
	for done < want {
		pos := uint64(off) + uint64(done)
		block := pos / n.x.bs
		i := sort.Search(len(n.ext), func(i int) bool { return n.ext[i].logical+n.ext[i].length > block })
		span := uint64(want - done)
		physical := uint64(0)
		zero := true
		if i < len(n.ext) {
			v := n.ext[i]
			if block < v.logical {
				if gap := v.logical*n.x.bs - pos; gap < span {
					span = gap
				}
			} else {
				end := (v.logical + v.length) * n.x.bs
				if end-pos < span {
					span = end - pos
				}
				zero = v.zero
				physical = v.physical*n.x.bs + pos - v.logical*n.x.bs
			}
		}
		if span > 4<<20 {
			span = 4 << 20
		}
		dst := p[done : done+int(span)]
		if zero {
			clear(dst)
		} else {
			nread, e := n.x.r.ReadAt(dst, int64(physical))
			done += nread
			if e != nil {
				return done, e
			}
			if nread != len(dst) {
				return done, io.ErrUnexpectedEOF
			}
			continue
		}
		done += len(dst)
	}
	if done < len(p) {
		return done, io.EOF
	}
	return done, nil
}
func (n *inode) entries() (map[string]uint32, error) {
	if n.mode&0xf000 != 0x4000 {
		return nil, fmt.Errorf("不是目录")
	}
	if d := n.x.dirs[n]; d != nil {
		return d, nil
	}
	if n.size > 64<<20 {
		return nil, fmt.Errorf("directory too large")
	}
	out := map[string]uint32{}
	b := make([]byte, n.x.bs)
	for off := int64(0); off < n.size; off += int64(len(b)) {
		_, e := n.ReadAt(b, off)
		if e != nil && e != io.EOF {
			return nil, e
		}
		for i := 0; i+8 <= len(b); {
			id := le.Uint32(b[i:])
			size := int(le.Uint16(b[i+4:]))
			nl := int(b[i+6])
			if size < 8 || i+size > len(b) || nl > size-8 {
				return nil, fmt.Errorf("损坏目录")
			}
			name := string(b[i+8 : i+8+nl])
			if id != 0 && name != "." && name != ".." {
				if e := Safe(name); e != nil {
					return nil, e
				}
				out[name] = id
			}
			i += size
		}
	}
	if len(out) <= 4096 {
		if len(n.x.dirs) >= 64 {
			n.x.dirs = nil
		}
		if n.x.dirs == nil {
			n.x.dirs = map[*inode]map[string]uint32{}
		}
		n.x.dirs[n] = out
	}
	return out, nil
}
func (x *Ext4) node(p string) (*inode, error) {
	if e := Safe(p); e != nil {
		return nil, e
	}
	n, e := x.get(2)
	if p == "." {
		return n, e
	}
	for _, s := range strings.Split(p, "/") {
		if e != nil {
			return nil, e
		}
		d, err := n.entries()
		if err != nil {
			return nil, err
		}
		id, ok := d[s]
		if !ok {
			return nil, fmt.Errorf("找不到 %s: %w", p, fs.ErrNotExist)
		}
		n, e = x.get(id)
	}
	return n, e
}
func (x *Ext4) List(p string) ([]Entry, error) {
	n, e := x.node(p)
	if e != nil {
		return nil, e
	}
	ds, e := n.entries()
	if e != nil {
		return nil, e
	}
	var out []Entry
	for name, id := range ds {
		n, e := x.get(id)
		if e != nil {
			return nil, e
		}
		if kind := n.mode & 0xf000; kind != 0x4000 && kind != 0x8000 {
			return nil, fmt.Errorf("拒绝特殊 inode")
		}
		out = append(out, Entry{name, n.size, n.mode&0xf000 == 0x4000})
	}
	return out, nil
}

// Keep seek/ReaderAt available for bounded streaming; SectionReader owns no fd.
type extentFile struct{ *io.SectionReader }

func (*extentFile) Close() error { return nil }

func (x *Ext4) Open(p string) (io.ReadCloser, error) {
	n, e := x.node(p)
	if e != nil {
		return nil, e
	}
	if n.mode&0xf000 != 0x8000 {
		return nil, fmt.Errorf("不是普通文件")
	}
	return &extentFile{io.NewSectionReader(n, 0, n.size)}, nil
}
func (x *Ext4) Close() error {
	if x.close != nil {
		return x.close()
	}
	return nil
}

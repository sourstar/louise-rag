// VeGa §6.1 before the node (docs/notes/vega.md): does a float32 survive
// LocalAI's JSON on its way to Louise?
//
// LocalAI v4.11.0 writes an embedding as encoding/json writes a []float32
// (core/schema/openai.go, Item): the shortest decimal that reads back to the
// float32, 'f' or 'e' by the ES6 cutoffs, "-0" for negative zero. Louise reads
// it with Python's json (correctly rounded to float64) and the vector digest
// narrows it to float32 (struct.pack('<f'), round to nearest even). Two
// roundings in a row; this program finds every value where they lose the bits.
//
// No Go on the development PC; the official image, by version:
//
//	docker run --rm -v "$PWD:/w" -w /w golang:1.26-alpine go run tools/vega_f32_json.go
//	    every finite float32 (4,278,190,080), all cores, about 4 minutes:
//	    prints the values that do not come back, and the count
//	docker run --rm -v "$PWD:/w" -w /w golang:1.26-alpine go run tools/vega_f32_json.go -golden \
//	    > tests/fixtures/vega/go_f32_json.txt
//	    a fixed sample written by encoding/json itself, `<bits hex> <json>` a
//	    line, which tests/vega/test_f32_json.py reads back
//
// Measured 2026-10-08 with go1.26.8: TWO values, ±7.038531e-26 (bits
// 15ae43fd, 95ae43fd), read back as their even neighbour; app/vega/tag.py
// repairs exactly those two. Negative zero is not in the count: Go reads "-0"
// back as -0.0, Python's json as the integer 0 (app/vega/client.py keeps it).
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"math"
	"math/rand/v2"
	"os"
	"runtime"
	"strconv"
	"sync"
)

// encode is encoding/json's floatEncoder for 32 bits (go1.26 encode.go).
func encode(f float32) string {
	abs := math.Abs(float64(f))
	fm := byte('f')
	if abs != 0 && (float32(abs) < 1e-6 || float32(abs) >= 1e21) {
		fm = 'e'
	}
	b := strconv.AppendFloat(nil, float64(f), fm, -1, 32)
	if fm == 'e' {
		n := len(b)
		if n >= 4 && b[n-4] == 'e' && b[n-3] == '-' && b[n-2] == '0' {
			b[n-2] = b[n-1]
			b = b[:n-1]
		}
	}
	return string(b)
}

// survives says whether Python's reading gives the bits back.
func survives(bits uint32, s string) bool {
	d, err := strconv.ParseFloat(s, 64)
	return err == nil && math.Float32bits(float32(d)) == bits
}

func exhaustive() {
	n := runtime.NumCPU()
	var wg sync.WaitGroup
	var mu sync.Mutex
	var bad, checked uint64
	total := uint64(1) << 32
	chunk := total / uint64(n)
	for w := 0; w < n; w++ {
		wg.Add(1)
		go func(w int) {
			defer wg.Done()
			lo, hi := uint64(w)*chunk, uint64(w+1)*chunk
			if w == n-1 {
				hi = total
			}
			var lb, lc uint64
			for b := lo; b < hi; b++ {
				f := math.Float32frombits(uint32(b))
				if math.IsNaN(float64(f)) || math.IsInf(float64(f), 0) {
					continue
				}
				lc++
				if s := encode(f); !survives(uint32(b), s) {
					lb++
					mu.Lock()
					fmt.Printf("lost bits=%08x %s\n", b, s)
					mu.Unlock()
				}
			}
			mu.Lock()
			bad, checked = bad+lb, checked+lc
			mu.Unlock()
		}(w)
	}
	wg.Wait()
	fmt.Printf("cpus=%d checked=%d lost=%d\n", n, checked, bad)
}

func golden() {
	edges := []uint32{
		0x00000000, 0x80000000, // ±0
		0x00000001, 0x007fffff, 0x00800000, // smallest subnormal, largest subnormal, smallest normal
		0x7f7fffff, 0xff7fffff, // ±max
		0x358637bd, 0x358637be, 0x358637bc, // around 1e-6, the 'e' cutoff
		0x6258d726, 0x6258d727, 0x6258d725, // around 1e21, the other cutoff
		0x15ae43fd, 0x95ae43fd, 0x15ae43fc, 0x15ae43fe, // the two that are lost, and neighbours
		0x3f800000, 0x3dcccccd, 0x4b800000, 0x4b800001, // 1, 0.1, 2^24, 2^24+2
	}
	r := rand.New(rand.NewPCG(2026, 1008))
	var bits []uint32
	bits = append(bits, edges...)
	for len(bits) < 2000 { // any pattern: mostly tiny and huge magnitudes
		b := r.Uint32()
		if f := math.Float32frombits(b); !math.IsNaN(float64(f)) && !math.IsInf(float64(f), 0) {
			bits = append(bits, b)
		}
	}
	for i := 0; i < 2000; i++ { // what an embedding holds
		bits = append(bits, math.Float32bits(float32(r.NormFloat64()*0.05)))
	}
	for _, b := range bits {
		f := math.Float32frombits(b)
		raw, err := json.Marshal([]float32{f})
		if err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
		s := string(raw[1 : len(raw)-1])
		if s != encode(f) {
			fmt.Fprintf(os.Stderr, "encode() is not encoding/json for %08x: %s vs %s\n", b, s, encode(f))
			os.Exit(1)
		}
		fmt.Printf("%08x %s\n", b, s)
	}
}

func main() {
	g := flag.Bool("golden", false, "write the fixed sample instead of the exhaustive check")
	flag.Parse()
	if *g {
		golden()
		return
	}
	exhaustive()
}

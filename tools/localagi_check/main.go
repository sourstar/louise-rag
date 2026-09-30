// LocalAGI's own knowledge-base client (pkg/localrag) against a Louise node:
// the contract check of migration step 15d, run for real (07 §7.4).
//
// Built INSIDE a LocalAGI checkout, so it is exactly the client an agent uses:
//
//	tools/localagi_check/run.sh <localagi-checkout> <louise-url> <deposit-token-file> [source-url]
//
// Verified 2026-09-30 against LocalAGI 7e0947d: exit 0, every call answered.
//
// It does what an agent does — create its collection at start, store two
// memories, count, search — then what the Web UI and compaction do: list
// collections and entries, read one, delete it, list sources, reset, and
// create again (refused; LocalAGI ignores that error). Exit 0 and a JSON
// report, or exit 1 naming the call that failed.
package main

import (
	"encoding/json"
	"fmt"
	"os"
	"strings"
	"time"

	"github.com/mudler/LocalAGI/pkg/localrag"
)

func must(err error, what string) {
	if err != nil {
		fmt.Fprintf(os.Stderr, "FAIL %s: %v\n", what, err)
		os.Exit(1)
	}
}

func main() {
	url := os.Args[1]
	// the token by PATH (invariant 8): never on a command line, where any
	// process listing shows it
	raw, err := os.ReadFile(os.Args[2])
	must(err, "reading the deposit token file")
	key := strings.TrimSpace(string(raw))
	out := map[string]any{}
	wc := localrag.NewWrappedClient(url, key, "Louise-Check-Agent")
	must(wc.Store("The user prefers espresso without sugar."), "Store 1")
	must(wc.Store("The user's cat is called Miso."), "Store 2")
	out["count"] = wc.Count()
	res, err := wc.Search("espresso", 3)
	must(err, "Search")
	if len(res) == 0 {
		must(fmt.Errorf("no result"), "Search")
	}
	out["search"] = res
	c := localrag.NewClient(url, key)
	cols, err := c.ListCollections()
	must(err, "ListCollections")
	out["collections"] = cols
	entries, err := c.ListEntries("louise-check-agent")
	must(err, "ListEntries")
	out["entries"] = entries
	content, chunks, err := c.GetEntryContent("louise-check-agent", entries[0])
	must(err, "GetEntryContent")
	out["content"], out["chunks"] = content, chunks
	remaining, err := c.DeleteEntry("louise-check-agent", entries[0])
	must(err, "DeleteEntry")
	out["remaining"] = remaining
	srcs, err := c.ListSources("louise-check-agent")
	must(err, "ListSources")
	out["sources"] = srcs
	// a URL source, when one is given: added, fetched by the node's worker,
	// listed with its last fetch, then removed (06 §6.7)
	if len(os.Args) > 3 {
		src := os.Args[3]
		must(c.AddSource("louise-check-agent", src, 60), "AddSource")
		deadline := time.Now().Add(90 * time.Second)
		for {
			ls, err := c.ListSources("louise-check-agent")
			must(err, "ListSources")
			if len(ls) == 1 && ls[0].LastUpdate != "" {
				out["source"] = ls[0]
				break
			}
			if time.Now().After(deadline) {
				must(fmt.Errorf("not fetched within 90s: %+v", ls), "source fetch")
			}
			time.Sleep(500 * time.Millisecond)
		}
		ents, err := c.ListEntries("louise-check-agent")
		must(err, "ListEntries after the source")
		out["entries_with_source"] = ents
		must(c.RemoveSource("louise-check-agent", src), "RemoveSource")
		left, err := c.ListSources("louise-check-agent")
		must(err, "ListSources after removal")
		out["sources_after_removal"] = left
	}
	must(wc.Reset(), "Reset")
	out["count_after_reset"] = wc.Count()
	out["create_again_err"] = fmt.Sprint(c.CreateCollection("louise-check-agent"))
	b, _ := json.MarshalIndent(out, "", "  ")
	fmt.Println(string(b))
}

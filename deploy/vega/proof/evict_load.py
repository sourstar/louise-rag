"""VeGa §6.2 eviction proof: use one LocalAI model (a 1-token chat, or an
embedding) while polling /system and the card every 100 ms; print the
timeline of loaded-model changes and the card's minimum free memory.

    load.py MODEL chat|embed [SECONDS_AFTER]
"""
import json, re, sys, threading, time, urllib.request

LOCALAI = "http://127.0.0.1:30286"


def post(path, body, timeout=600):
    req = urllib.request.Request(LOCALAI + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.load(r)


def loaded():
    with urllib.request.urlopen(LOCALAI + "/system", timeout=10) as r:
        return {m["id"] for m in json.load(r).get("loaded_models", [])}


def free_mib():
    for line in open("/sys/kernel/debug/dri/0/vram0_mm"):
        m = re.search(r"free:\s*(\d+)MiB", line)
        if m:
            return int(m.group(1))


def main():
    model, kind = sys.argv[1], sys.argv[2]
    after = float(sys.argv[3]) if len(sys.argv) > 3 else 8
    t0 = time.monotonic()
    events, stop = [], threading.Event()
    state = {"min_free": free_mib()}

    def poll():
        prev = loaded()
        events.append((0.0, "start", sorted(prev)))
        while not stop.is_set():
            time.sleep(0.1)
            try:
                cur = loaded()
            except Exception as e:  # noqa: BLE001
                events.append((time.monotonic() - t0, "poll-error", str(e)[:80]))
                continue
            f = free_mib()
            state["min_free"] = min(state["min_free"], f)
            if cur != prev:
                events.append((time.monotonic() - t0, f"+{sorted(cur - prev)} -{sorted(prev - cur)}",
                               f"free {f} MiB"))
                prev = cur

    th = threading.Thread(target=poll, daemon=True)
    th.start()
    s = time.monotonic()
    if kind == "chat":
        st, body = post("/v1/chat/completions", {"model": model, "max_tokens": 1,
                        "messages": [{"role": "user", "content": "Say OK."}]})
    else:
        st, body = post("/v1/embeddings", {"model": model, "input": ["a short text"]})
    events.append((time.monotonic() - t0, f"answer {st} after {time.monotonic() - s:.2f} s", ""))
    time.sleep(after)
    stop.set()
    th.join()
    for t, what, extra in events:
        print(f"  t={t:7.2f}s  {what}  {extra}")
    print(f"  min card free during: {state['min_free']} MiB")


if __name__ == "__main__":
    main()

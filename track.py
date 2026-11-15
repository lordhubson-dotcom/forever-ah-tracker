"""Hourly WoW Forever auction tracker for GitHub Actions (stdlib only).

One run = wait for Blizzard's next hourly snapshot, download every auction house (EU, Forever), compare each house
with the previous snapshot (kept in a state file between runs) and write one compact file of per-item numbers:

    python track.py --state state.json.gz --out out [--wait 420] [--houses 111,117]

Needs BLIZZARD_CLIENT_ID / BLIZZARD_CLIENT_SECRET in the environment. Writes out/h-<fetched UTC>.json.gz and the new
state file only when at least one house had a new snapshot. The comparison rules are the desk's tracker.py ones:

  p (prices) item, listings, qty, min, p10, p25, median                       (quantity-weighted unit prices)
  f (flows)  item, new_qty, new_listings, gone_qty, gone_listings, sold_qty, sold_value, partial_qty, repost_qty
             gone = vanished although it could not have expired yet (it had LONG/VERY_LONG left an hour ago);
             repost = a gone auction matched by a new one of the same item and quantity (cancel + relist);
             sold = gone, not reposted, priced at or under last hour's median and (stackables) not dearer than a
             cheaper auction that survived untouched; plus partial buys of commodity auctions
  x (ladder) item, new_under_listings, new_under_qty, L0..L4, G0..G4
             new_under = new auctions priced under last hour's cheapest (undercuts)
             L/G = units that could not expire / units of those that went (sold or cancelled, reposts excluded, partial
             buys included), by price vs last hour's cheapest: <=1.00x, <=1.05x, <=1.15x, <=1.35x, more
"""
import argparse, gzip, json, os, sys, time, urllib.error, urllib.parse, urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

REGION = "eu"
RULESET = 1                       # every ruleset returns the same houses
PRIORITY = [111, 117]             # Normal Alliance (Stormwind) and its Blackwater house first
PROBE = 125                       # a tiny house, cheap to ask whether a new snapshot is out
CANT_EXPIRE = {"LONG", "VERY_LONG"}
LADDER = (1.00, 1.05, 1.15, 1.35)
MAX_GAP_H = 1.9                   # a LONG auction has >= 2 h left: a longer gap could hide expiries
TL_CODE = {"SHORT": 0, "MEDIUM": 1, "LONG": 2, "VERY_LONG": 3}
TL_NAME = {v: k for k, v in TL_CODE.items()}


def log(*msg):
    print(datetime.now(timezone.utc).strftime("%H:%M:%S"), *msg, flush=True)


class Api:
    def __init__(self, cid, secret):
        self.cid, self.secret, self.token = cid, secret, None

    def _token(self):
        req = urllib.request.Request("https://oauth.battle.net/token", data=b"grant_type=client_credentials")
        import base64
        req.add_header("Authorization", "Basic " + base64.b64encode(f"{self.cid}:{self.secret}".encode()).decode())
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)["access_token"]

    def get(self, path, since=None):
        """-> (status, last_modified, json or None)"""
        url = (f"https://{REGION}.api.blizzard.com{path}?"
               + urllib.parse.urlencode({"namespace": f"dynamic-forever-{REGION}", "locale": "en_US"}))
        for attempt in range(5):
            if not self.token:
                self.token = self._token()
            h = {"Authorization": f"Bearer {self.token}", "Accept-Encoding": "gzip"}
            if since:
                h["If-Modified-Since"] = since
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=180) as r:
                    body = r.read()
                    if r.headers.get("Content-Encoding") == "gzip":
                        body = gzip.decompress(body)
                    return r.status, r.headers.get("last-modified"), json.loads(body)
            except urllib.error.HTTPError as e:
                if e.code == 304:
                    return 304, since, None
                if e.code == 404:
                    return 404, None, None
                if e.code == 401:
                    self.token = None
                if attempt == 4:
                    raise
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                if attempt == 4:
                    raise
            time.sleep(3 * 2 ** attempt)


def iso(lm):
    return parsedate_to_datetime(lm).astimezone(timezone.utc).isoformat(timespec="seconds")


def hours(a, b):
    return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() / 3600


def quantiles(pairs):
    pairs.sort()
    total = sum(n for _, n in pairs)
    out, run, i = [], 0, 0
    for q in (0.10, 0.25, 0.50):
        target = q * total
        while i < len(pairs) and run + pairs[i][1] < target:
            run += pairs[i][1]
            i += 1
        out.append(pairs[min(i, len(pairs) - 1)][0])
    return out


def to_state(listings):
    """{auction id: (item, qty, unit price, time-left code)}"""
    st = {}
    for x in listings:
        if "unit_price" in x:
            up = x["unit_price"]
        elif x.get("buyout"):
            up = x["buyout"] // max(1, x.get("quantity") or 1)
        else:
            continue                                   # bid-only auction: no price to compare
        st[x["id"]] = (x["item"]["id"], x.get("quantity") or 1, up, TL_CODE.get(x.get("time_left", ""), -1))
    return st


def bucket(up, cheapest):
    r = up / cheapest if cheapest else 1e9
    for i, edge in enumerate(LADDER):
        if r <= edge:
            return i
    return len(LADDER)


def compare(state, prev):
    """-> (prices rows, flows rows, ladder rows); flows/ladder empty when prev is None."""
    by_item = defaultdict(list)
    for item, qty, up, _ in state.values():
        by_item[item].append((up, qty))
    prices = []
    for item, pairs in by_item.items():
        p10, p25, med = quantiles(pairs)
        prices.append([item, len(pairs), sum(n for _, n in pairs), min(p for p, _ in pairs), p10, p25, med])
    if prev is None:
        return prices, [], []

    pb = defaultdict(list)
    for item, qty, up, _ in prev.values():
        pb[item].append((up, qty))
    prev_med = {item: quantiles(pairs)[2] for item, pairs in pb.items()}
    prev_min = {item: min(p for p, _ in pairs) for item, pairs in pb.items()}
    nb = len(LADDER) + 1
    f = defaultdict(lambda: [0] * 8)               # new_qty new_l gone_qty gone_l sold_qty sold_val partial repost
    x = defaultdict(lambda: [0, 0] + [0] * (2 * nb))   # new_under_l new_under_q L0..L4 G0..G4
    new_by_item = defaultdict(lambda: defaultdict(int))
    for aid, (item, qty, up, tl) in state.items():
        if aid not in prev:
            f[item][0] += qty
            f[item][1] += 1
            new_by_item[item][qty] += 1
            if item in prev_min and up < prev_min[item]:
                x[item][0] += 1
                x[item][1] += qty
        else:
            pq = prev[aid][1]
            if qty < pq:                               # partial commodity buy
                f[item][6] += pq - qty
                f[item][4] += pq - qty
                f[item][5] += (pq - qty) * up
                if TL_NAME.get(prev[aid][3]) in CANT_EXPIRE:
                    x[item][2 + nb + bucket(prev[aid][2], prev_min[item])] += pq - qty
    cheapest_left, stackable = {}, set()
    for aid, (item, qty, up, tl) in prev.items():
        if qty > 1:
            stackable.add(item)
        cur = state.get(aid)
        if cur and cur[1] == qty and up < cheapest_left.get(item, float("inf")):
            cheapest_left[item] = up
    for aid, (item, qty, up, tl) in prev.items():
        if TL_NAME.get(tl) not in CANT_EXPIRE:
            continue
        b = bucket(up, prev_min[item])
        x[item][2 + b] += qty
        if aid in state:
            continue
        f[item][2] += qty
        f[item][3] += 1
        if new_by_item[item].get(qty, 0) > 0:      # same item and quantity re-listed this hour: cancel + relist
            new_by_item[item][qty] -= 1
            f[item][7] += qty
            continue
        x[item][2 + nb + b] += qty
        if up <= prev_med.get(item, up) and (item not in stackable or up <= cheapest_left.get(item, float("inf"))):
            f[item][4] += qty
            f[item][5] += qty * up
    flows = [[item, *v] for item, v in f.items()]
    ladder = [[item, *v] for item, v in x.items() if any(v)]
    return prices, flows, ladder


def load_state(path):
    if not path or not os.path.exists(path):
        return {}
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        d = json.load(fh)
    return {int(h): {"ts": v["ts"], "state": {a[0]: tuple(a[1:]) for a in v["a"]}} for h, v in d["houses"].items()}


def save_state(path, st):
    out = {"v": 1, "houses": {str(h): {"ts": v["ts"], "a": [[aid, *t] for aid, t in v["state"].items()]}
                              for h, v in st.items()}}
    with gzip.open(path + ".tmp", "wt", encoding="utf-8", compresslevel=5) as fh:
        json.dump(out, fh, separators=(",", ":"))
    os.replace(path + ".tmp", path)


def scan_house(api, hid, prev):
    s1, lm1, d1 = api.get(f"/data/wow/ruleset/{RULESET}/auctions/{hid}")
    s2, lm2, d2 = api.get(f"/data/wow/ruleset/{RULESET}/auctions/{hid}/commodities")
    listings = ((d1 or {}).get("auctions") or []) + ((d2 or {}).get("auctions") or [])
    if not listings or not (lm1 or lm2):
        return None, None
    ts = iso(lm1 or lm2)
    if prev and ts == prev["ts"]:
        return "same", None
    state = to_state(listings)
    ok = bool(prev) and 0 < hours(prev["ts"], ts) < MAX_GAP_H
    note = ""
    if ok and len(state) < 0.75 * len(prev["state"]):      # a sudden 25% drop: probably an incomplete answer
        ok, note = False, f" (only {len(state)} vs {len(prev['state'])} listings - not compared)"
    prices, flows, ladder = compare(state, prev["state"] if ok else None)
    rec = {"house": hid, "ts": ts, "prev_ts": prev["ts"] if prev else None, "listings": len(state), "diffed": ok,
           "p": prices, "f": flows, "x": ladder}
    log(f"house {hid}: {len(state)} listings @ {ts}, {len(prices)} items, compared={ok}{note}")
    return rec, state


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default="state.json.gz")
    ap.add_argument("--out", default="out")
    ap.add_argument("--wait", type=int, default=420, help="seconds to wait for a new snapshot")
    ap.add_argument("--houses", default="", help="only these houses (comma list), for testing")
    args = ap.parse_args()
    cid, secret = os.environ.get("BLIZZARD_CLIENT_ID"), os.environ.get("BLIZZARD_CLIENT_SECRET")
    if not cid or not secret:
        sys.exit("BLIZZARD_CLIENT_ID / BLIZZARD_CLIENT_SECRET are not set")
    api = Api(cid, secret)
    st = load_state(args.state)

    idx = api.get(f"/data/wow/ruleset/{RULESET}/auctions/")[2] or {}
    houses = [h["id"] for h in idx.get("auctions", [])]
    if args.houses:
        houses = [int(h) for h in args.houses.split(",")]
    houses.sort(key=lambda h: (h not in PRIORITY, PRIORITY.index(h) if h in PRIORITY else 0, h))
    log(f"{len(houses)} houses; state has {len(st)} (newest {max((v['ts'] for v in st.values()), default='-')})")

    deadline = time.time() + args.wait
    if PROBE in st and PROBE in houses:            # wait until Blizzard publishes a snapshot we haven't recorded
        while True:
            s, lm, _ = api.get(f"/data/wow/ruleset/{RULESET}/auctions/{PROBE}")
            if lm and iso(lm) != st[PROBE]["ts"]:
                break
            if time.time() > deadline:
                log(f"no new snapshot yet (still {st[PROBE]['ts']}) - nothing to do")
                return
            time.sleep(45)

    records, pending = [], list(houses)
    while pending:
        later = []
        for hid in pending:
            rec, state = scan_house(api, hid, st.get(hid))
            if rec == "same":
                later.append(hid)
            elif rec:
                records.append(rec)
                st[hid] = {"ts": rec["ts"], "state": state}
        if not later or time.time() > deadline or not records:
            if later:
                log(f"houses still on the old snapshot, left for the next run: {later}")
            break
        pending = later
        time.sleep(30)

    if not records:
        log("nothing new recorded")
        return
    os.makedirs(args.out, exist_ok=True)
    name = "h-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + ".json.gz"
    with gzip.open(os.path.join(args.out, name), "wt", encoding="utf-8", compresslevel=9) as fh:
        json.dump({"v": 1, "region": REGION, "fetched": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   "houses": records}, fh, separators=(",", ":"))
    save_state(args.state, st)
    size = os.path.getsize(os.path.join(args.out, name))
    log(f"wrote {name} ({size // 1024} KB, {len(records)} houses, "
        f"{sum(r['diffed'] for r in records)} compared with the previous hour)")


if __name__ == "__main__":
    main()

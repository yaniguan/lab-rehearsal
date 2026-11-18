"""Recall -> rehearse -> verify -> run -> write back, for liquid-handling protocols.

A protocol is a JSON list of steps (pick_up_tip, aspirate, dispense, drop_tip, mix)
on a virtual deck: a 96-well plate (plate:A1..H12, max 300 uL per well), a 12-column
reservoir (res:1..12, max 15000 uL per column), a rack of 96 tips and a
single-channel pipette (20-300 uL).

Each protocol is checked against past runs in memory/runs.jsonl, rehearsed on a
simulated deck, passed through a default-deny verify gate, run by a mock executor
only if approved, and the outcome is appended to memory/runs.jsonl.
"""
import hashlib, json, os
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
PROTOCOL_DIR = os.path.join(HERE, "protocols")
MEMORY = os.path.join(HERE, "memory", "runs.jsonl")

WELL_MAX, RES_MAX = 300, 15000
PIP_MIN, PIP_MAX = 20, 300
TIP_RACK = 96
START = {"res:1": (10000, "buffer"), "res:2": (2000, "sample")}
LIQUID_OPS = {"aspirate", "dispense", "mix"}
KNOWN_OPS = LIQUID_OPS | {"pick_up_tip", "drop_tip"}


def new_deck():
    deck = {}
    for row in "ABCDEFGH":
        for col in range(1, 13):
            deck[f"plate:{row}{col}"] = {"vol": 0.0, "max": WELL_MAX, "liquids": set()}
    for col in range(1, 13):
        vol, liquid = START.get(f"res:{col}", (0, None))
        deck[f"res:{col}"] = {"vol": float(vol), "max": RES_MAX,
                              "liquids": {liquid} if liquid else set()}
    return deck


def protocol_hash(steps):
    text = json.dumps(steps, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def load_memory():
    if not os.path.exists(MEMORY):
        return []
    with open(MEMORY) as f:
        return [json.loads(line) for line in f if line.strip()]


def recall(h):
    """Return a warning line based on earlier runs with the same hash."""
    past = [r for r in load_memory() if r["hash"] == h]
    if not past:
        return "no earlier runs of this protocol"
    failed = [r for r in past if not r["passed"]]
    if not failed:
        return f"passed before in run(s) {[r['run'] for r in past]}"
    rules = sorted({v.split()[2].rstrip(":") for r in failed for v in r["violations"]})
    return f"WARNING: failed before in run(s) {[r['run'] for r in failed]} ({', '.join(rules)})"


def rehearse(steps):
    """Simulate every step; record the facts the verify rules need."""
    deck = new_deck()
    tip, tips_left, content, touched = False, TIP_RACK, 0.0, set()
    trace = []
    for i, step in enumerate(steps, 1):
        op, loc, vol = step.get("op"), step.get("loc"), float(step.get("vol", 0))
        well = deck.get(loc)
        fact = {"i": i, "op": op, "loc": loc, "vol": vol, "tip": tip,
                "known": op in KNOWN_OPS and (op not in LIQUID_OPS or well is not None)}
        trace.append(fact)
        if not fact["known"]:
            continue
        if op == "pick_up_tip":
            fact["tips_left"] = tips_left
            if not tip and tips_left > 0:
                tip, tips_left, content, touched = True, tips_left - 1, 0.0, set()
        elif op == "drop_tip":
            tip, content, touched = False, 0.0, set()
        else:
            fact.update(available=well["vol"], in_pipette=content,
                        touched=set(touched), liquids=set(well["liquids"]))
            if op == "aspirate":
                fact["pipette_after"] = content + vol
                take = min(vol, well["vol"])
                well["vol"] -= take
                content += take
            elif op == "dispense":
                give = min(vol, content)
                content -= give
                well["vol"] += give
                well["liquids"] |= touched
                fact.update(dest_after=well["vol"], max=well["max"])
            touched |= well["liquids"]
    plate = {k: v["vol"] for k, v in deck.items() if k.startswith("plate:") and v["vol"] > 0}
    return trace, {"tip_on": tip, "tips_used": TIP_RACK - tips_left, "plate": plate}


# ---- verify rules: each returns a message on violation, else None ----
def volume_in_range(f):
    if f["op"] in LIQUID_OPS and not PIP_MIN <= f["vol"] <= PIP_MAX:
        return f"{f['vol']:g} uL outside pipette range {PIP_MIN}-{PIP_MAX} uL"
    if f.get("pipette_after", 0) > PIP_MAX:
        return f"pipette would hold {f['pipette_after']:g} uL > {PIP_MAX} uL"


def no_overfill(f):
    if f["op"] == "dispense" and f["dest_after"] > f["max"]:
        return f"{f['loc']} would hold {f['dest_after']:g} uL > {f['max']} uL"


def enough_liquid(f):
    if f["op"] in ("aspirate", "mix") and f["vol"] > f["available"]:
        return f"{f['op']} {f['vol']:g} uL from {f['loc']} holding {f['available']:g} uL"
    if f["op"] == "dispense" and f["vol"] > f["in_pipette"]:
        return f"dispense {f['vol']:g} uL but pipette holds {f['in_pipette']:g} uL"


def tip_present(f):
    if f["op"] in LIQUID_OPS and not f["tip"]:
        return f"{f['op']} without a tip"
    if f["op"] == "pick_up_tip" and (f["tip"] or f["tips_left"] == 0):
        return "tip already attached" if f["tip"] else "tip rack empty"


def no_tip_reuse(f):
    if f["op"] == "aspirate" and f["tip"]:
        foreign = f["touched"] - f["liquids"]
        if foreign:
            return (f"tip carrying {sorted(foreign)} reused in {f['loc']} "
                    f"containing {sorted(f['liquids'])}")


STEP_RULES = [volume_in_range, no_overfill, enough_liquid, tip_present, no_tip_reuse]


def verify(trace, end):
    """Default-deny gate: approve only if every step is known and every rule passes."""
    violations, checked = [], 0
    for f in trace:
        if not f["known"]:
            violations.append(f"step {f['i']} known_step: unknown op/location {f['op']} {f['loc']}")
            continue
        for rule in STEP_RULES:
            msg = rule(f)
            if msg:
                violations.append(f"step {f['i']} {rule.__name__}: {msg}")
        checked += 1
    if end["tip_on"]:
        violations.append(f"step {len(trace)} all_tips_dropped: tip still attached at end")
    approved = bool(trace) and checked == len(trace) and not violations
    return approved, violations


def run(steps, approved):
    """Mock executor: only 'sends' commands when the gate approved the protocol."""
    if not approved:
        return "blocked by verify gate, nothing sent to the robot"
    sent = Counter(s["op"] for s in steps)
    return f"sent {sum(sent.values())} commands " + ", ".join(f"{k} x{v}" for k, v in sorted(sent.items()))


def write_back(name, h, approved, violations):
    run_id = len(load_memory()) + 1
    record = {"run": run_id, "protocol": name, "hash": h, "passed": approved, "violations": violations}
    with open(MEMORY, "a") as f:
        f.write(json.dumps(record) + "\n")
    return run_id


def main():
    os.makedirs(os.path.dirname(MEMORY), exist_ok=True)
    if os.path.exists(MEMORY):
        os.remove(MEMORY)
    names = sorted(n for n in os.listdir(PROTOCOL_DIR) if n.endswith(".json"))
    for pass_no in (1, 2):
        print(f"######## pass {pass_no} ########")
        for name in names:
            with open(os.path.join(PROTOCOL_DIR, name)) as f:
                steps = json.load(f)
            h = protocol_hash(steps)
            print(f"== {name} (hash {h}, {len(steps)} steps)")
            print(f"  RECALL:   {recall(h)}")
            trace, end = rehearse(steps)
            print(f"  REHEARSE: tips used {end['tips_used']}, plate wells filled {len(end['plate'])}, "
                  f"tip on at end {end['tip_on']}")
            approved, violations = verify(trace, end)
            print(f"  VERIFY:   {'PASS' if approved else 'FAIL'} ({len(violations)} violation(s))")
            for v in violations:
                print(f"    - {v}")
            print(f"  RUN:      {run(steps, approved)}")
            if approved:
                print("            final plate: " + ", ".join(
                    f"{k[6:]}={v:g}" for k, v in sorted(end["plate"].items())))
            run_id = write_back(name, h, approved, violations)
            print(f"  WRITE:    run #{run_id} appended to memory/runs.jsonl")
    records = load_memory()
    passed = sum(r["passed"] for r in records)
    print(f"######## memory: {len(records)} runs, {passed} passed, {len(records) - passed} failed")


if __name__ == "__main__":
    main()

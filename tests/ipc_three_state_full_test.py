# -*- coding: utf-8 -*-
"""Regression test for the 2026-07-31 'worker executes heartbeat-less' incident.

Reproduces the exact shape: a worker claims a task via the bash-fallback path
(one-shot recv, which deletes .alive on the way out), then works for longer than
the reaper's liveness margin WITHOUT arming a watcher and WITHOUT calling ack.

Pre-fix expectation : the task is requeued underneath the worker (state QUEUED).
Post-fix expectation: the task stays IN_PROGRESS, because claiming it spawned a
                      detached busy-beater.

HOW TO RUN (Windows-only harness: uses taskkill/powershell):
  1. Copy this file into a directory whose NAME CONTAINS "ipctest" and that is
     OUTSIDE any Claude Code project. The script refuses to run otherwise: it
     resolves an isolated mailbox off its own directory, and a guard aborts if
     the resolved mailbox is not an isolated test one.
  2. Unset CLAUDE_PROJECT_DIR for the run (second refusal guard).
  3. Point IPC_PY at the ipc.py under test, e.g.:
       set IPC_PY=C:/path/to/claude-star-ipc/ipc.py
       python ipc_three_state_full_test.py
Groups 1-8: 2026-07-31 heartbeat-split regression. Groups 9-13: 2026-08-01
three-state (QUEUED-BUSY) coverage — note-type, ALL-with-busy, ghost-busy bounded
window, status/send window consistency, end-state leak guard.

Runs in this directory, so it resolves its own isolated mailbox (ipc.py keys the
DB off cwd) and cannot touch the live A/C terminals.
"""
import os, subprocess, sys, time, json, io
sys.stdout.reconfigure(encoding='utf-8')

_BUSY_FRESH_WAIT = 9  # > 3*_BUSY_POLL(6s) freshness window
# Resolve the ipc.py under test: IPC_PY env var wins (set it when you copy this
# file out of the repo); otherwise the repo-layout default (tests/../ipc.py).
IPC = os.environ.get("IPC_PY") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ipc.py")
HERE = os.path.dirname(os.path.abspath(__file__))
FAILED = []

def run(*args, expect_rc=None):
    p = subprocess.run([sys.executable, IPC, *args], cwd=HERE,
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = (p.stdout or "").strip()
    if expect_rc is not None and p.returncode != expect_rc:
        FAILED.append(f"rc: {' '.join(args)} -> {p.returncode}, expected {expect_rc}")
    return out, p.returncode

def check(label, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}{('  ' + detail) if detail else ''}")
    if not cond:
        FAILED.append(label)

print("=== 0. isolated mailbox ===")
# Pre-flight, BEFORE writing anything. ipc.py keys the mailbox off the PROJECT
# ROOT (walking up from cwd, and preferring CLAUDE_PROJECT_DIR), not off cwd — so
# running this from inside a real project, or with that variable set, would send
# the test's traffic into a LIVE mailbox. Refuse rather than pollute.
if os.environ.get("CLAUDE_PROJECT_DIR"):
    sys.exit("REFUSING: CLAUDE_PROJECT_DIR is set; the mailbox would resolve to that project.")

# Start from an empty mailbox. Leftovers from a previous run are not a harmless
# annoyance: open tasks from the last run keep a beater alive and shift the timing
# the later groups measure. Observed once — group 8's stand-down moved 22s -> 60s
# purely from residue, while its assertion still read as a code defect.
import shutil, importlib.util
os.chdir(HERE)
_spec = importlib.util.spec_from_file_location("ipc_probe", IPC)
_m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_m)
_state = _m._STATE_DIR
if "ipctest" not in _state:
    sys.exit(f"REFUSING: resolved mailbox is not an isolated test one: {_state}")
subprocess.run(["powershell", "-NoProfile", "-Command",
                "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
                # Narrowed from '*busy-daemon*' to '*busy-daemon*--me TW*': the test
                # mailbox only ever spawns --me TW beaters, while the LIVE worker C
                # (running this very test) has a '*busy-daemon*--me C*' process in a
                # DIFFERENT mailbox. The broad glob would kill C's live beater and the
                # reaper would reap this in-flight task mid-run. (Task #668 step 2.)
                "Where-Object { $_.CommandLine -like '*busy-daemon*--me TW*' } | "
                "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"],
               capture_output=True)
time.sleep(2)
shutil.rmtree(os.path.dirname(_state), ignore_errors=True)

out, _ = run("init")
print(" ", out)
statedir = os.path.dirname(out.split("db=")[-1].strip())  # dirname: 'db=' gives the FILE
# Positive assertion, not a negative string match against one project's hash:
# the resolved key must be derived from THIS directory. Project-independent.
key = os.path.basename(os.path.dirname(statedir))
check("mailbox key derives from this test dir (not any live project)",
      key.startswith(os.path.basename(HERE) + "-"), f"key={key}")

print("\n=== 1. dispatch a task to TW (no watcher ever armed) ===")
out, _ = run("send", "--from", "A", "--to", "TW", "hello work", "--lease", "600")
print(" ", out)
tid = out.split("#")[1].split()[0]

print("\n=== 2. worker claims it via one-shot recv (bash-fallback shape) ===")
out, _ = run("recv", "--me", "TW")
check("task was delivered", "hello work" in out, out[:60])

busy = os.path.join(statedir, "_worker_TW.busy")
alive = os.path.join(statedir, "_watcher_TW.alive")
check("busy heartbeat created by the claim", os.path.exists(busy))
check("watcher heartbeat NOT created (worker is not parked)", not os.path.exists(alive))

out, rc = run("status", "--watch", "TW")
check("status reports BUSY, not DOWN", "BUSY" in out, out)
check("status exit code still gates dispatch (non-zero)", rc == 1, f"rc={rc}")

print("\n=== 3. work for longer than the reaper margin (24s) without ack ===")
for i in range(4):
    time.sleep(8)
    out, _ = run("pending", "--hub", "A", "--detail")
    state = out.split("[")[-1].split("]")[0] if "[" in out else "?"
    fresh = time.time() - os.path.getmtime(busy) if os.path.exists(busy) else 999
    print(f"  t+{(i+1)*8:>2}s  state={state:<12} busy-beat age={fresh:.1f}s")
    if (i + 1) * 8 >= 24:
        check(f"t+{(i+1)*8}s: still IN_PROGRESS (not requeued)", state == "IN_PROGRESS", state)

print("\n=== 4. worker finishes ===")
out, _ = run("done", "--me", "TW", "--task", tid)
print(" ", out)
deadline = time.time() + 12
while time.time() < deadline and os.path.exists(busy):
    time.sleep(1)
check("busy heartbeat cleared within ~12s of done", not os.path.exists(busy))
out, _ = run("status", "--watch", "TW")
check("status back to DOWN", "DOWN" in out, out)
out, _ = run("pending", "--hub", "A")
check("hub sees fan-out complete", "NONE" in out, out[:60])

print("\n=== 5. require-watcher must QUEUE to a busy-but-unparked worker (2026-08-01 semantics) ===")
# 2026-08-01 change: a fresh .busy is accepted liveness evidence — the message
# queues (QUEUED-BUSY, rc=0) instead of refusing. Refusal is reserved for
# neither-parked-nor-busy (asserted in group 6 after the beater dies).
run("send", "--from", "A", "--to", "TW", "second", "--lease", "600")
run("recv", "--me", "TW")   # busy again
out, rc = run("send", "--from", "A", "--to", "TW", "third", "--require-watcher")
check("require-watcher queues to busy worker (QUEUED-BUSY, rc=0)",
      rc == 0 and "QUEUED-BUSY" in out, f"rc={rc} {out[:70]}")
qb_id = out.split("#")[1].split()[0] if "#" in out else None
out, _ = run("pending", "--hub", "A", "--detail")
check("the queued-busy message is visible as a QUEUED task in pending",
      "QUEUED" in out, out[:120])
# C review B-6 (2026-08-01): resending the same submit_id at a busy worker must
# collapse onto the existing row (DUP with the SAME id), not double-queue via
# the busy path.
out1, _ = run("send", "--from", "A", "--to", "TW", "idem", "--require-watcher", "--submit-id", "K1")
first_id = out1.split("#")[1].split()[0] if "#" in out1 else None
out, rc = run("send", "--from", "A", "--to", "TW", "idem", "--require-watcher", "--submit-id", "K1")
dup_id = out.split("#")[1].split()[0] if "#" in out else None
check("submit-id dup x queued-busy collapses to DUP on the SAME row",
      rc == 0 and "DUP" in out and dup_id == first_id,
      f"rc={rc} first=#{first_id} dup=#{dup_id} {out[:60]}")
# Cancel the probe messages so later groups' pending-state parsing sees the same
# mailbox shape as before this group existed.
for pid_ in (qb_id, dup_id):
    if pid_:
        run("cancel", "--task", pid_, "--by", "A")

print("\n=== 6. counter-proof: kill the beater, the reaper must STILL requeue ===")
# The fix must not amount to disabling the reaper. With the busy heartbeat gone
# (worker genuinely died), a stale claim has to be requeued exactly as before.
ident = json.load(io.open(busy, encoding="utf-8")) if os.path.exists(busy) else {}
pid = ident.get("pid")
if pid:
    subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
try:
    os.remove(busy)
except OSError:
    pass
time.sleep(2)
check("busy heartbeat is gone after killing the beater", not os.path.exists(busy))
# 2026-08-01: with neither .alive nor .busy, require-watcher must refuse again —
# the busy-queue relaxation must not have widened into queue-to-anyone.
out, rc = run("send", "--from", "A", "--to", "TW", "probe-dead", "--require-watcher")
check("require-watcher refuses when neither parked nor busy (rc=3)",
      rc == 3 and "REFUS" in out.upper(), f"rc={rc} {out[:60]}")
deadline = time.time() + 40
state = "?"
while time.time() < deadline:
    time.sleep(5)
    o, _ = run("pending", "--hub", "A", "--detail")
    state = o.split("[")[-1].split("]")[0] if "[" in o else "?"
    if state == "QUEUED":
        break
check("dead worker's task IS requeued (reaper still load-bearing)",
      state == "QUEUED", f"state={state}")

def close_all_open_tw():
    """done every non-terminal A->TW task. Hardcoded ids broke silently the first
    time an extra row shifted the sequence (2026-08-01: the QUEUED-BUSY probe in
    group 5 moved every later id by one and group 8's `done 3,4,5` left a
    600s-lease task open, so the beater legitimately outlived the 55s window and
    the standdown assertion failed with NO code defect). Parse pending instead."""
    o, _ = run("pending", "--hub", "A", "--detail")
    for line in o.splitlines():
        if "->TW" in line and "#" in line and ("QUEUED" in line or "IN_PROGRESS" in line or "STALE" in line):
            tid_ = line.split("#")[1].split()[0]
            run("done", "--me", "TW", "--task", tid_)

print("\n=== 7. a dead beater must be REPLACED on the next claim ===")
# Regression for the spawn-guard bug. Flow: close the task left open by step 5,
# dispatch and claim a fresh one (this spawns a beater), KILL that beater but
# leave its .busy file behind, let the file age past the freshness window, then
# claim another task. If the guard's window were the daemon lifetime (the
# original bug) rather than a few poll intervals, the stale file would suppress
# the replacement beater for hours while _lease_alive (24s margin) already reads
# the worker as dead.
close_all_open_tw()
run("send", "--from", "A", "--to", "TW", "fourth", "--lease", "600")
run("recv", "--me", "TW")
ident = json.load(io.open(busy, encoding="utf-8")) if os.path.exists(busy) else {}
pid = ident.get("pid")
if pid:
    subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
time.sleep(1)
# Leave the STALE FILE IN PLACE (this is what the buggy guard tripped over) and
# let it age past the freshness window.
check("stale .busy still on disk after killing the beater", os.path.exists(busy))
time.sleep(_BUSY_FRESH_WAIT)
run("send", "--from", "A", "--to", "TW", "fifth", "--lease", "600")
run("recv", "--me", "TW")          # next claim must spawn a REPLACEMENT beater
time.sleep(3)
age = time.time() - os.path.getmtime(busy) if os.path.exists(busy) else 999
check("replacement beater is beating again", age < 6, f"beat age={age:.1f}s")

print("\n=== 8. orphan beater must stop beating once the lease ceiling falls ===")
# The dangerous shape the review named (M2): the worker's SESSION dies but the
# detached beater survives, so .busy keeps saying 'alive' and the reaper never
# redelivers. The daemon must therefore stand down of its own accord once every
# task it is beating for has passed its hard ceiling — bounding the orphan window
# to the lease instead of to the runaway cap. Simulated by claiming a task under
# a short lease and then doing nothing at all, as a dead session would.
close_all_open_tw()
time.sleep(3)
out, _ = run("send", "--from", "A", "--to", "TW", "orphan", "--lease", "20")
run("recv", "--me", "TW")
check("beater running for the short-lease task", os.path.exists(busy))
t_claim = time.time()
deadline = t_claim + 60
while time.time() < deadline and os.path.exists(busy):
    time.sleep(2)
elapsed = time.time() - t_claim
check("beater stood down after the ceiling fell (not at the 4h cap)",
      not os.path.exists(busy) and elapsed < 55, f"stood down at t+{elapsed:.0f}s (lease was 20s)")
state = "?"
deadline = time.time() + 45
while time.time() < deadline:
    o, _ = run("pending", "--hub", "A", "--detail")
    state = o.split("[")[-1].split("]")[0] if "[" in o else "?"
    if state == "QUEUED":
        break
    time.sleep(5)
check("and the dead worker's task is redelivered", state == "QUEUED", f"state={state}")

# === Groups 9-12: the four edge cases C named untested in review #665 ==========
# A applied the B-1/B-3 fixes (busy window uses --max-age; note CAUTION; QUEUED-BUSY
# hint mentions reaper doesn't touch unclaimed rows). These groups assert those
# fixes and the remaining documented boundaries (B-2 ghost-busy, B-5 ALL-with-busy).

print("\n=== 9. note-type QUEUED-BUSY (C review B-3) ===")
# A note queued to a busy worker must: rc=0, print QUEUED-BUSY + the note CAUTION,
# NOT appear in pending, and still be delivered on the next recv (fire-and-forget
# but not lost).
run("send", "--from", "A", "--to", "TW", "g9-task", "--lease", "600")
run("recv", "--me", "TW")                      # busy again (beater spawned)
out, rc = run("send", "--from", "A", "--to", "TW", "g9-note",
              "--require-watcher", "--type", "note")
check("note QUEUED-BUSY rc=0", rc == 0 and "QUEUED-BUSY" in out, f"rc={rc} {out[:80]}")
check("note CAUTION fragment present (exempt from the reaper)",
      "exempt from the reaper" in out, out[:140])
pout, _ = run("pending", "--hub", "A", "--detail")
check("note row NOT visible in pending (notes are fire-and-forget)",
      "g9-note" not in pout, f"note body leaked into pending: {pout[:120]}")
out, _ = run("recv", "--me", "TW")
check("note delivered on next recv (not lost)", "g9-note" in out, out[:80])
close_all_open_tw()

print("\n=== 10. ALL with busy (C review B-5) ===")
# Forge a registry: TW (busy, no .alive) + TX (no heartbeat at all). ALL must
# expand to include TW (busy = live) and exclude TX (double-nothing filtered).
reg_path = os.path.join(statedir, "ipc_roles.json")
with open(reg_path, "w", encoding="utf-8") as _rf:
    json.dump({"TW": {"session_id": "test-tw"}, "TX": {"session_id": "test-tx"}}, _rf)
run("send", "--from", "A", "--to", "TW", "g10-task", "--lease", "600")
run("recv", "--me", "TW")                      # TW busy, no .alive
out, rc = run("send", "--from", "A", "--to", "ALL", "g10-bcast", "--require-watcher")
check("ALL broadcast reached busy TW", "TW" in out and ("QUEUED-BUSY" in out or "SENT" in out),
      f"rc={rc} {out[:140]}")
check("ALL excluded double-nothing TX (no heartbeat)", "TX" not in out, out[:140])
try:
    os.remove(reg_path)
except OSError:
    pass
close_all_open_tw()

print("\n=== 11. ghost-busy bounded window (C review B-2, known boundary) ===")
# Worker claims a short-lease task then "dies" (we do nothing — the detached
# beater outlives the session). Three phases:
#  (1) within the lease, send --require-watcher gets QUEUED-BUSY — the orphan
#      beater fools the gate. This RECORDS the known boundary, not a bug (B-2 was
#      documented as a bounded self-heal, not fixed).
#  (2) the daemon self-extinguishes at the lease ceiling (~22s for 20s lease).
#  (3) after self-extinguish, the same send becomes REFUSED — the window closed.
close_all_open_tw()
time.sleep(3)
run("send", "--from", "A", "--to", "TW", "g11-ghost", "--lease", "20")
run("recv", "--me", "TW")
check("beater running for the short-lease ghost task", os.path.exists(busy))
out, rc = run("send", "--from", "A", "--to", "TW", "g11-probe-window",
              "--require-watcher")
check("phase 1: ghost-busy fools gate into QUEUED-BUSY (known boundary, not bug)",
      rc == 0 and "QUEUED-BUSY" in out, f"rc={rc} {out[:60]}")
t_claim = time.time()
deadline = t_claim + 55
while time.time() < deadline and os.path.exists(busy):
    time.sleep(2)
elapsed = time.time() - t_claim
check("phase 2: orphan beater self-extinguished after lease ceiling (not 4h cap)",
      not os.path.exists(busy) and elapsed < 55, f"stood down at t+{elapsed:.0f}s")
out, rc = run("send", "--from", "A", "--to", "TW", "g11-probe-after",
              "--require-watcher")
check("phase 3: window closed — REFUSED after beater gone",
      rc == 3 and "REFUS" in out.upper(), f"rc={rc} {out[:60]}")
close_all_open_tw()

print("\n=== 12. window consistency: status vs send (C review B-1 fix) ===")
# B-1 fix: send and status BOTH judge busy on --max-age (default 8), not the old
# _BUSY_FRESH(6) for send. Kill the beater, let .busy age, and at two samples
# (7s = fresh, 10s = stale) assert status --watch and send --require-watcher AGREE
# at the same moment with the same --max-age. Old bug: status BUSY but send REFUSED.
close_all_open_tw()
time.sleep(3)
run("send", "--from", "A", "--to", "TW", "g12-task", "--lease", "600")
run("recv", "--me", "TW")
# Wait for the detached daemon to stamp .busy with ITS pid. recv stamps .busy
# synchronously with the RECV process's pid then exits; the daemon overwrites it
# within ~2s. Reading too soon kills the already-dead recv pid, leaves the daemon
# beating .busy fresh, and defeats the aging test (first run: both samples read
# "alive" because .busy never aged).
time.sleep(3)
ident = json.load(io.open(busy, encoding="utf-8")) if os.path.exists(busy) else {}
pid = ident.get("pid")
if pid:
    subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
# Verify the beater is actually dead: .busy mtime must stop advancing. If it
# advanced, a daemon survived (race) — kill the current pid and re-check.
mt0 = os.path.getmtime(busy) if os.path.exists(busy) else 0
time.sleep(3)
mt1 = os.path.getmtime(busy) if os.path.exists(busy) else 0
if mt1 > mt0:
    ident = json.load(io.open(busy, encoding="utf-8")) if os.path.exists(busy) else {}
    if ident.get("pid"):
        subprocess.run(["taskkill", "/PID", str(ident["pid"]), "/F"],
                       capture_output=True)
    time.sleep(1)
check("stale .busy left after killing beater (no finally on /F)", os.path.exists(busy))
beaten_at = os.path.getmtime(busy) if os.path.exists(busy) else time.time()
for target_age, label in ((7, "fresh<8"), (10, "stale>8")):
    while time.time() - beaten_at < target_age:
        time.sleep(0.3)
    sout, _ = run("status", "--watch", "TW", "--max-age", "8")
    send_out, send_rc = run("send", "--from", "A", "--to", "TW", "g12-probe",
                            "--require-watcher", "--max-age", "8")
    status_alive = "BUSY" in sout          # no .alive -> never ALIVE; BUSY = busy-heartbeat fresh
    send_alive = (send_rc == 0 and "QUEUED-BUSY" in send_out)
    agree = (status_alive == send_alive)
    check(f"sample {label} (age~{target_age}s): status and send agree (both {'alive' if status_alive else 'dead'})",
          agree, f"status={sout.strip()[:24]} send_rc={send_rc} {send_out[:40]}")
    if send_rc == 0 and "#" in send_out:
        run("cancel", "--task", send_out.split("#")[1].split()[0], "--by", "A")
    time.sleep(2)
try:
    os.remove(busy)
except OSError:
    pass
close_all_open_tw()

print("\n=== 13. regression guard (full-script run; original 1-8 must still be green) ===")
# Group 13 is a run-discipline requirement: the whole script runs end-to-end, so
# groups 1-8 execute before 9-12 and must stay green (the harness does not let us
# run only the new groups). This block adds a final state-leak check — no open
# A->TW tasks — as a structural guard that the appended groups did not corrupt
# the mailbox the earlier groups rely on.
out, _ = run("pending", "--hub", "A", "--detail")
leak = [l for l in out.splitlines()
        if "->TW" in l and ("QUEUED" in l or "IN_PROGRESS" in l or "STALE" in l
                            or "NEEDS-REVIEW" in l)]
check("no open A->TW tasks leaked after all 13 groups", not leak, f"leaked: {leak}")

print("\n" + ("ALL PASS" if not FAILED else f"FAILURES ({len(FAILED)}):"))
for f in FAILED:
    print("  -", f)
sys.exit(1 if FAILED else 0)

# Session store durability: what the quality gates are, and what improving them costs

**Question.** Can the durability/integrity guarantees of the per-Session on-disk store —
`exchanges.ndjson`, `bodies/`, `session.json` — be improved, and at what cost?

**Answer, in one paragraph.** Yes, in layers. The cheap wins are atomic manifest writes
(write-temp-then-rename, plus a directory fsync), a torn-tail contract for the NDJSON log,
and a schema-version key in the manifest. The deliberate decision is fsync policy: the store
flushes every record to the kernel but never fsyncs, and on macOS even `fsync()` is not a
durability guarantee — Apple's own man page says so and points at `fcntl(F_FULLFSYNC)`. How
much syncing to add is a latency-versus-durability trade on a single event loop that also runs
the proxy, so it should be measured before it is chosen. The heavyweight option, replacing the
log with SQLite in WAL mode, buys real atomic-commit and integrity-check machinery but breaks
the documented property that the log is "tailable by anything that is not this process", so it
is a genuine conflict with a prior decision and is analysed rather than recommended.

Nothing here modifies code. This is a survey of options against primary sources, with each
option's conflict with AGENTS.md's "Decisions already made" stated where one exists.

## Current guarantees, grounded in the code

What the store already promises, reading `src/https_proxy_mcp/capture/store.py`:

- **One line per Exchange, flushed per record.** `_append_to_log` writes a line and calls
  `flush()` (`store.py:283-286`). Flush pushes Python's buffer into the kernel; it is not an
  fsync. Python's `os` documentation states the pairing explicitly — `f.flush()`, then
  `os.fsync(f.fileno())`, to get internal buffers *to disk*
  ([docs.python.org/3/library/os.html#os.fsync](https://docs.python.org/3/library/os.html#os.fsync)).
  Only the first half happens today.
- **No fsync anywhere in `capture/`.** The one fsync in the codebase is the system-proxy
  sentinel's (`targets/systemproxy.py:578-583`), which already implements write-scratch, flush,
  `os.fsync`, `os.replace` — the exact pattern Option 2 proposes for the manifests, so the
  project has an in-repo precedent rather than a new idiom to introduce. So for the store,
  "durability" today means: survives process exit, `kill -9`, and interpreter crash (the kernel
  holds the bytes after flush); does **not** survive kernel panic or power loss with any
  guarantee, and on macOS does not survive even with an fsync added — see below.
- **Ordering invariant: bodies are spilled before the log line is appended.** `_spill` runs
  during `Exchange` construction (`store.py:256-257`) and `_append_to_log` runs after
  (`store.py:262`). A crash between the two leaves an orphan body file (harmless; nothing
  references it) and can never leave a log line whose `BodyRef` points at a missing file. This
  is the store's one real crash-safety property and it is currently implicit — no test pins it.
- **Bodies are written non-atomically.** `(self._dir / relative).write_bytes(stored)`
  (`store.py:274`) — open, write, close, no fsync, no temp-then-rename. A crash mid-write
  leaves a truncated body file, but see the ordering invariant: the log line that would cite it
  never lands, so the inconsistency is invisible to any correct reader.
- **The manifest is written non-atomically, once.** `session.json` is
  `write_text(json.dumps(...))` (`capture/session.py:120`); `run.json` is the same shape
  (`runs/registry.py:170`). Both are write-once (session) or write-twice (run) files, so the
  torn-write window is narrow, but it exists and the files are small enough that atomic
  replacement is essentially free.
- **Retention prunes by name, never opening files** (`session.py:137-158`). Session ids are
  microsecond timestamps, so a corrupt or missing manifest cannot break pruning. This is a
  deliberate, good property (`AGENTS.md`, P1 table) that any change must preserve.
- **Permissions are the security gate, and they are tested.** `0700` on the session and bodies
  directories (`store.py:91-92`), asserted by `tests/test_store.py:291-293`.
- **Single event loop, no lock, by decision** (`store.py:19-21`, ADR-0001). Every improvement
  below is evaluated against this: anything that blocks the loop for tens of milliseconds per
  record is not a store change, it is an architecture change.
- **Crash recovery exists for the *machine*, not for the store.** `tests/test_kill9_recovery.py`
  proves a `kill -9`'d server leaves a sentinel the next start can repair. There is no
  equivalent for the data: no test kills a writer mid-append and inspects what the log looks
  like afterwards.

For reference, the format contract is JSON Lines: UTF-8, one valid JSON value per line, `\n`
terminator ([jsonlines.org](https://jsonlines.org/)). The per-record round-trip
(`to_log_dict`/`from_log_dict`, `capture/exchange.py:155,181`) exists and is tested, but no
*file* reader does — that is v1.1 candidate #8 (`docs/v1.1-candidates.md`), which is where most
of these gates would first be exercised.

## Option 1: an explicit fsync / F_FULLFSYNC policy on the log and bodies

**Recommendation.** Adopt `os.fsync` at `close()` unconditionally, plus a periodic fsync
(every N records or T seconds, whichever first) if measurement shows per-record fsync is too
slow. Do **not** fsync per record by default, and do **not** use `F_FULLFSYNC` per record at
all. If power-loss durability ever becomes a stated requirement, gate `F_FULLFSYNC`-at-close
behind an option.

**Evidence.** macOS is the deviant platform here, and Apple says so in its own man page:
`fsync()` "will flush all data from the host to the drive (i.e. the 'permanent storage
device'), [but] the drive itself may not physically write the data to the platters for quite
some time and it may be written in an out-of-order sequence… This is not a theoretical edge
case" — and for stricter guarantees, "Mac OS X provides the F_FULLFSYNC fcntl"
([fsync(2), Apple](https://developer.apple.com/library/archive/documentation/System/Conceptual/ManPages_iPhoneOS/man2/fsync.2.html)).
`F_FULLFSYNC` "does the same thing as fsync(2) then asks the drive to flush all buffered data
to the permanent storage device", and "the operation may take quite a while to complete"
([fcntl(2), Apple](https://developer.apple.com/library/archive/documentation/System/Conceptual/ManPages_iPhoneOS/man2/fcntl.2.html)).
POSIX leaves the whole thing implementation-defined — "the nature of the transfer is
implementation-defined", and the rationale notes a *null implementation* is conforming
([POSIX fsync](https://pubs.opengroup.org/onlinepubs/9699919799/functions/fsync.html)).
So the guarantee ladder is: `flush()` (today) < `os.fsync` < `fcntl(F_FULLFSYNC)`, with the
last two meaningfully different **only on macOS** and the last one expensive everywhere.

SQLite's own configuration is the calibration point: its `fullfsync` pragma defaults to **off**
even though "Only Mac OS X supports F_FULLFSYNC"
([sqlite.org/pragma.html#pragma_fullfsync](https://sqlite.org/pragma.html#pragma_fullfsync)),
and its corruption documentation's advice for surviving lying drives is to use WAL mode and
checkpoint rarely — not to fullfsync every commit
([sqlite.org/howtocorrupt.html, §3.1](https://sqlite.org/howtocorrupt.html)).

**Cost.** `os.fsync` per record is one syscall per Exchange — microseconds on an SSD for the
kernel flush, but on the shared event loop every synchronous call is latency the proxy also
feels. `F_FULLFSYNC` per record is the "quite a while" case and would stall capture on a busy
run. A periodic policy (N=50 records or T=5s) bounds the loss window to seconds of traffic
while amortizing the syscall. None of this has been measured in this repo; the first step is a
benchmark, not a policy.

**Conflicts with prior decisions.** None directly, but the single-event-loop decision
(ADR-0001) is what makes per-record full sync an architecture question rather than a one-liner.
Flag, don't dismiss.

## Option 2: atomic manifest writes for `session.json` and `run.json`

**Recommendation.** Adopt. Write to a temp file in the same directory, fsync it, `os.replace`
onto the target, fsync the directory.

**Evidence.** Rename atomicity is a POSIX requirement — "the action of the function shall be
atomic" ([POSIX rename](https://pubs.opengroup.org/onlinepubs/9699919799/functions/rename.html)),
and Python exposes it as `os.replace`: "If successful, the renaming will be an atomic operation
(this is a POSIX requirement)" ([docs.python.org/3/library/os.html#os.replace](https://docs.python.org/3/library/os.html#os.replace)).
Atomicity of the rename is not durability of the directory entry, though: the Linux `fsync(2)`
man page (macOS's man pages do not document this either way, which is itself a reason to do it)
states that fsyncing a file "does not necessarily ensure that the entry in the directory
containing the file has also reached disk. For that an explicit fsync() on a file descriptor
for the directory is also needed"
([man7.org/fsync.2](https://man7.org/linux/man-pages/man2/fsync.2.html)). SQLite's atomic-commit
protocol does exactly this — "the directory that contains the super-journal is also synced in
order to make sure the super-journal file will appear in the directory following a power
failure" ([sqlite.org/atomiccommit.html, §7](https://sqlite.org/atomiccommit.html)) — which is
the pattern to copy.

**Cost.** A few lines in a shared helper; manifests are written once or twice per Session, so
the fsyncs are free. No loop-latency concern. The sentinel's `_write_sentinel`
(`targets/systemproxy.py:563-583`) is the working in-repo example of the pattern, minus the
directory fsync.

**Conflicts.** None. It strengthens what `session.py:140-141` already promises ("a Session
whose manifest is missing or corrupt still sorts and still prunes") by making "corrupt"
rarer, without touching the pruning path.

## Option 3: a torn-tail contract for the NDJSON log

**Recommendation.** Adopt as a documented contract, enforced where the future file reader
lands (v1.1 candidate #8): **the last line of `exchanges.ndjson` may be partial; all other
lines must be valid.** The reader truncates to the last newline and reports how much was
dropped. Do not add per-record checksums or length prefixes.

**Evidence.** The format itself makes this tractable: a JSON Lines file is "one valid JSON
value per line", and a trailing terminator after the last value is "strongly recommended"
([jsonlines.org](https://jsonlines.org/)) — this store always writes it (`store.py:285`), so
truncation at the last `\n` is lossless-with-respect-to-complete-records by construction. A
partial final line can only be the record in flight when the writer died, because appends are
sequential and flushed per record. Anything stronger (CRC per line, length framing) makes the
file no longer NDJSON and breaks the property the store's docstring advertises: "tailable by
anything that is not this process" (`store.py:11-12`).

A middle option exists if mid-file corruption (disk rot, not crash) ever matters: a `_crc`
field *inside* each JSON object keeps the file valid NDJSON while letting a reader detect
corrupt lines anywhere. That is insurance against a failure mode SQLite classifies as real but
rare ("disks will occasionally flip a bit in the middle of a sector",
[sqlite.org/howtocorrupt.html §4](https://sqlite.org/howtocorrupt.html)) and is not worth the
field until there is a reader to check it.

**Cost.** Documentation plus one function in the eventual loader. The write path is unchanged.

**Conflicts.** None — it is the minimum contract the already-planned reader needs anyway.

## Option 4: schema versioning on the manifest and the log

**Recommendation.** Adopt a `"schema": 1` key in `session.json` (and `run.json`) the next time
either is touched. The NDJSON lines need no version while `from_log_dict` tolerates unknown
keys; add one only when a breaking change is actually made.

**Evidence.** Internal: `Session.to_manifest` (`session.py:123-134`) and
`Exchange.from_log_dict` (`exchange.py:181`) are the two seams a future reader parses. v1.1
candidate #8 notes the read halves for *records* exist but no `from_manifest` does
(`docs/v1.1-candidates.md`, §8). A version key is what lets that future reader say "this
directory predates me" instead of failing on a missing `lens` key. No external citation —
this is a design judgement, stated as one.

**Cost.** One key, one test asserting it. Free.

**Conflicts.** None.

## Option 5: directory fsync at Session creation

**Recommendation.** Adopt, folded into Option 2's helper: after `Session.create` makes the
directory and writes the manifest, fsync the parent. The interesting case is not the manifest
but the *directory itself*: an unsynced `sessions/` entry can lose a whole just-created
Session on power failure even if every file in it was synced.

**Evidence.** Same directory-entry sources as Option 2
([man7.org/fsync.2](https://man7.org/linux/man-pages/man2/fsync.2.html);
[sqlite.org/atomiccommit.html](https://sqlite.org/atomiccommit.html)).

**Cost.** One syscall, once per Session. Free.

**Conflicts.** None.

## Option 6: batching the flush

**Recommendation.** Do not change. The current per-record `flush()` is a visibility guarantee,
not a durability one — `_append_to_log`'s docstring says it: "so a tailer sees it without
waiting on a buffer" (`store.py:284`). Batching it would trade the tailable property for
buffering that fsync-policy work (Option 1) makes redundant: a periodic fsync already implies
a periodic flush boundary if ever wanted.

**Evidence.** Python buffers text files in chunks (default policy: fixed-size buffer, "on most
systems… typically 128 kilobytes"; line buffering only for interactive streams) —
[docs.python.org/3/library/functions.html#open](https://docs.python.org/3/library/functions.html#open).
Without the explicit flush, up to a buffer's worth of completed Exchanges would be invisible to
external tailers and to a crashed-session reader.

**Cost of changing it.** Loss of a documented property. None of the fsync options require it.

**Conflicts.** Changing it would conflict with the tailable-log decision above. Not changing
it conflicts with nothing.

## Option 7: replace the log with SQLite in WAL mode

**Recommendation.** Do not adopt now. Revisit only if (a) the v1.1 reader makes NDJSON parsing
the bottleneck, or (b) mid-file corruption detection becomes a real requirement. This is the
one option that *removes* a documented property, so it is analysed honestly and then set aside.

**What it would buy, per SQLite's own documentation.** Atomic commit with automatic rollback
after crash or power failure — "the partially written transaction should be automatically
rolled back the next time the database file is accessed… fully automatic"
([sqlite.org/howtocorrupt.html](https://sqlite.org/howtocorrupt.html)). In WAL mode
specifically: readers do not block writers and a writer does not block readers; "WAL uses many
fewer fsync() operations and is thus less vulnerable to problems on systems where the fsync()
system call is broken"; and a failed sync during COMMIT "might result in loss of durability
but not in a corrupt database file" — corruption is only possible during checkpoint
([sqlite.org/wal.html](https://sqlite.org/wal.html);
[sqlite.org/howtocorrupt.html §3.1](https://sqlite.org/howtocorrupt.html)). Built-in
verification via `PRAGMA integrity_check`, which checks out-of-sequence entries, misformatted
records, missing pages, freelist integrity and more, returning `'ok'` when clean
([sqlite.org/pragma.html#pragma_integrity_check](https://sqlite.org/pragma.html#pragma_integrity_check)).
And Python's stdlib `sqlite3` is a complete DB-API binding with live-database backup support
(`Connection.backup` "works even if the database is being accessed by other clients")
([docs.python.org/3/library/sqlite3.html](https://docs.python.org/3/library/sqlite3.html)),
so no new dependency.

**What it would cost.**
- **The tailable log.** `store.py:11-12` and ADR-0005's consequence ("Other consumers can tail
  the Session's `exchanges.ndjson`") describe a file `tail -f` can read. A WAL database is
  two extra files (`-wal`, `-shm`), and a *stale* `-wal` copied without its database loses or
  corrupts committed transactions
  ([sqlite.org/wal.html §4](https://sqlite.org/wal.html)) — exactly the failure mode
  `docs/v1.1-candidates.md` §8's "Sessions are retained ten deep, so the material is normally
  still there" workflow would trip over. This conflicts with a documented decision; flagged,
  not dismissed.
- **New failure modes the current design does not have.** POSIX advisory locks, hot-journal
  pairing, cross-process `SQLITE_BUSY` cases — sqlite.org's corruption page is a catalogue of
  them ([sqlite.org/howtocorrupt.html](https://sqlite.org/howtocorrupt.html)). And WAL had a
  genuine, corruption-causing race ("the WAL-reset bug") present from 3.7.0 through 3.51.2,
  fixed only in 3.51.3 (2026-03-13) ([sqlite.org/wal.html §11](https://sqlite.org/wal.html)) —
  so the bundled SQLite version in whatever Python the user installs becomes a quality gate of
  its own (`sqlite3.sqlite_version` reports it). The repo's `requires-python = ">=3.12"`
  (`pyproject.toml:9`) does not pin the SQLite version at all.
- **A rewrite of the store and its test suite**, including re-deciding where bodies live
  (BLOBs in the db, or files plus foreign keys reintroducing the orphan problem the current
  ordering invariant already solves).

**What stays true either way.** Retention by directory name (`session.py:137-158`) and the
`0700` boundary are orthogonal to the storage engine.

## The verification side: what the tests do and do not cover

Today `tests/test_store.py` pins the round-trip, eviction accounting, truncation marking,
permissions, and post-close behaviour — everything except crash behaviour.
`tests/test_kill9_recovery.py` is the suite's one crash-injection test, and it injects into
the *proxy sentinel*, not the store. Two additions would close that gap, in order of value:

1. **A kill-9 store test.** A child process records Exchanges in a loop and is SIGKILLed
   mid-run; the parent then asserts the contract from Option 3: every line up to the last
   newline parses via `from_log_dict`, a partial tail (if any) is only the final line, and no
   log line references a body file that does not exist (the ordering invariant). This mirrors
   `test_kill9_recovery.py`'s proven shape — real child, real SIGKILL, no handlers — and pins
   the one implicit crash property the store already has.
2. **A torn-manifest test**, once Option 2 lands: write a truncated `session.json`, assert the
   loader (when it exists, per v1.1 #8) rejects it cleanly and `session_ids` still prunes the
   directory — the property `session.py:140-141` already promises.

What *not* to attempt: testing that fsync actually survives power loss. POSIX's own rationale
concedes "the key aspects of fsync() are unreasonable to test in a test suite" and a real
conformance test "should probably force a system crash (power shutdown)"
([POSIX fsync, RATIONALE](https://pubs.opengroup.org/onlinepubs/9699919799/functions/fsync.html)).
SQLite tests this with simulated power-failure and I/O-error harnesses built into its own test
infrastructure ([sqlite.org/howtocorrupt.html §8](https://sqlite.org/howtocorrupt.html));
replicating that here would cost more than every improvement above combined. Test the protocol
(what is written, in what order, what a reader tolerates), not the hardware.

## Suggested next steps, ranked by effort against payoff

1. **Option 2 + 5 (atomic manifests, directory fsync).** Hours. Removes the only torn-write
   window in the Session metadata, at zero runtime cost.
2. **Option 3 + kill-9 store test (torn-tail contract, pinned).** A day at most. Turns the
   store's one implicit crash property into an enforced one, and pre-answers the hard question
   for v1.1 candidate #8's loader.
3. **Option 4 (schema key).** Minutes, do it the next time the manifest is touched anyway.
4. **Option 1 (fsync policy).** Half a day, but benchmark first: per-record `os.fsync` on the
   shared loop, periodic fsync, and fsync-at-close, measured during a busy capture. Adopt
   fsync-at-close unconditionally; adopt periodic only if per-record is measurably too slow and
   the loss window matters. Leave `F_FULLFSYNC` out until power-loss durability is a stated
   requirement — Apple's man page's "quite a while" and SQLite's default-off `fullfsync` both
   say it is not the default shape.
5. **Option 7 (SQLite WAL).** Defer. Revisit only if the NDJSON reader of v1.1 #8 becomes the
   bottleneck or mid-file corruption detection becomes a requirement; it trades away the
   documented tailable log, and the WAL-reset bug history means it also makes the bundled
   SQLite version a new release-time quality gate.

## Sources

- Apple, `fsync(2)` man page — https://developer.apple.com/library/archive/documentation/System/Conceptual/ManPages_iPhoneOS/man2/fsync.2.html
- Apple, `fcntl(2)` man page (F_FULLFSYNC) — https://developer.apple.com/library/archive/documentation/System/Conceptual/ManPages_iPhoneOS/man2/fcntl.2.html
- POSIX.1-2017, `fsync` — https://pubs.opengroup.org/onlinepubs/9699919799/functions/fsync.html
- POSIX.1-2017, `rename` — https://pubs.opengroup.org/onlinepubs/9699919799/functions/rename.html
- Linux man-pages project, `fsync(2)` (directory-entry durability) — https://man7.org/linux/man-pages/man2/fsync.2.html
- Python, `os` (`os.fsync`, `os.replace`) — https://docs.python.org/3/library/os.html#os.fsync , https://docs.python.org/3/library/os.html#os.replace
- Python, built-in `open` (buffering policy) — https://docs.python.org/3/library/functions.html#open
- Python, `sqlite3` — https://docs.python.org/3/library/sqlite3.html
- SQLite, Write-Ahead Logging — https://sqlite.org/wal.html
- SQLite, Atomic Commit — https://sqlite.org/atomiccommit.html
- SQLite, How To Corrupt An SQLite Database File — https://sqlite.org/howtocorrupt.html
- SQLite, PRAGMAs (`fullfsync`, `integrity_check`) — https://sqlite.org/pragma.html#pragma_fullfsync , https://sqlite.org/pragma.html#pragma_integrity_check
- JSON Lines specification — https://jsonlines.org/

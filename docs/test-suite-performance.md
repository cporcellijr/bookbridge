# Test-suite performance investigation

Measured 2026-11-13 in the isolated `experiment/test-suite-profiling` worktree,
initially based on `69c86cbc`. The approved implementation was then updated to
local dev `6dce3694` before verification and merge.

## Measured full runs

| Run | Elapsed, including process startup | Result |
| --- | ---: | --- |
| Existing serial pytest | 8m 54s | 7,830 passed, 15 skipped, 1,409 subtests passed |
| Two workers, whole files together | 4m 51s | 3 failures, 63 setup errors; another agent's full suite overlapped |
| Two workers, no overlapping suite, label fixes applied | 3m 51s | 7,830 passed, 15 skipped, 1,413 subtests passed |
| Four workers, whole files together | 2m 16s | 7,830 passed, 15 skipped, 1,409 subtests passed |

The passing four-worker run was about 3.9 times faster. Its JUnit test-name and
class-name multiset exactly matches the serial run: the improvement did not come
from omitting tests. Summed individual case times were 513.91s serial and 489.93s
with four workers; most of the elapsed-time saving came from doing that work
concurrently. These are individual measurements, not a guarantee of future times.

Environment: Windows, Intel i7-13700K (16 cores / 24 logical processors), Python
3.13.12, installed pytest 9.0.2, pytest-xdist 3.8.0, execnet 2.1.2. The pytest
version predates this experiment and was outside pyproject.toml's old >=8,<9
range. No pytest upgrade was performed. The initial benchmarks used two extra
packages installed under the evidence directory. For the approved implementation,
xdist and execnet were also installed in the shared project venv, and the pytest
dev/CI bounds were aligned to >=9,<10 for the existing
[built-in subtest reporting](https://docs.pytest.org/en/9.0.x/how-to/subtests.html).

## Confirmed culprits

1. **The normal command runs every test serially.** Four workers with
   `--dist loadfile` passed the complete suite. This scheduling keeps each file's
   tests together, which fits the existing module-level environment fixtures.

2. **Independent agents share fixed temporary databases.** During the two-worker
   experiment, another full pytest process was observed. Windows refused deletion
   of `/tmp/test_koreader_book_status_sync/database.db`,
   `/tmp/test_kosync_filename_variants/database.db`,
   `/tmp/test_kosync_behind_push/database.db`, `/tmp/test_device_hash_pairs/database.db`,
   and `/tmp/test_kosync_empty_progress_contract/database.db`. Those five shared
   paths accounted for all 63 setup errors. A later collection-only attempt also
   encountered `/tmp/test_kosync/database.db` locked by the other suite. Some test
   modules delete these directories at import, so collecting tests can collide too.
   Separate worktrees do not isolate these absolute paths. Worker environments are
   separate processes; their `os.environ` dictionaries are not shared.

3. **Three subtest labels were not serializable.** Two exception-class labels in
   `tests/test_job_kind_isolation.py` and an exception/mock label in
   `tests/test_cwa_sync_cooldown.py` could not cross the worker connection. The
   saved change converts only those labels to strings. All assertions and actual
   exception objects used by the tests remain intact. The three targeted tests
   then passed with two workers (9 subtests), followed by the full four-worker pass.

4. **Mocked transcription still performs expensive real memory collection.**
   `AudioTranscriber.process_audio()` calls `gc.collect()` after every audio chunk.
   A profile collecting the full suite but selecting four tests recorded 68 calls
   from this method consuming 9.87s. In a separate controlled pair with torch loaded,
   two mocked transcription test bodies took 2.17s + 2.34s normally and 0.11s + 0.08s
   with only the transcriber's `gc` reference replaced temporarily. Both passed.
   The implementation now mocks this module reference in the two fake-provider
   test bases. Python's global collector and production cleanup remain real.

5. **A cache test misses its intended transcription-provider mock.**
   `test_partial_cache_triggers_redownload` imports `AudioTranscriber` from
   `utils.transcriber`, but patches `src.utils.transcriber.get_transcription_provider`.
   The full runs emitted HuggingFace model-cache warnings from this test. Its body
   took 5.32s serial and 5.70s with four workers. An outer mock on the actual
   `utils.transcriber` factory, using `supports_raw_audio=False`, made the same test
   pass in 0.09s of call time / 0.73s total, with the factory called exactly once and
   no model-cache warning. The implementation saves the corrected mock, gives this
   fixture its own temporary directory, and checks the exact expected failure and
   fake-provider calls instead of swallowing arbitrary exceptions.

There is also distributed setup cost: serial totals were 38.95s for diagnostics,
31.60s for authentication, 22.82s for reading-session aggregation, and 22.18s for
the new Library tests. The slowest individual test was a real migration-history
test at 18.70s. These measurements do not justify removing migration coverage or
changing SQLite durability settings.

## Implemented workflow

- `python tests/run_all_tests.py` uses four workers with `--dist loadfile`.
  Forwarded pytest arguments still work; `-n 0` selects a serial diagnostic run.
  CI uses the same entry point and retains its coverage arguments.
- A shared OS file lock in the user's temp directory is acquired by
  `tests/conftest.py` before collection, including direct and collect-only pytest
  commands. A second controller exits promptly with an actionable message (exit 4).
  Xdist workers skip the controller lock. Closing the handle or exiting the owner
  releases it; the lock file stays present, avoiding stale-PID and inode races.
- Keep focused tests during edits and one full run on the finished source state.
  Reuse a result only for the same source and dependency state, including dirty files.
- The three label fixes, correct provider mock, and narrow cleanup mocks preserve
  the original cache, ordering and coverage assertions. The cache test now also
  verifies the factory and transcription calls and uses a unique cleaned-up folder.
- Other fixed temporary paths remain protected by serialized controllers. Allowing
  unrelated suite jobs to overlap would require isolating those paths first.
  Older worktrees without the coordinator hook do not participate in this lock.

The lock has real Windows subprocess tests for contention, close, normal exit,
killed-owner release and rejection before collection. A separate Linux container
probe verified POSIX contention and release after close and killed-owner exit.
No production application code or SQLite durability setting was changed.

## Final verification on current local dev

- Updated to `6dce3694` before the implementation. The canonical runner passed
  **7,914 tests / 1,430 subtests / 15 skipped / 0 failures** in **134.70s wall**
  (133.32s reported by pytest). This comprises 7,850 existing tracked tests,
  seven new lock/runner tests, and 57 existing host-local Omni tests mirrored
  into the worktree for validation.
- The four-worker coverage probe passed **55 tests / 14 subtests** and produced
  coverage XML. Python 3.11 syntax checks and the independent POSIX lock probe passed.
- During the full run, a real second controller using `--collect-only` exited 4
  with the busy-run message. It did not collect tests.
- The cache-provider regression failed with the wrong namespace restored (zero
  calls to the intended factory mock), then passed after restoration. Both checks
  used an outer fake provider, preventing real model loading.
- In the final full run, total JUnit case times for the former slow metadata and
  resume tests were 0.084s and 0.087s. The cache test took 1.087s including setup
  and emitted no HuggingFace cache warning.

The shared venv now has the declared xdist and coverage tools. Local agent commands
and the read-only Omni allowlist use the new runner. The ignored Omni research
prompt also requires measured bottlenecks and exact mock bindings, and forbids
mocking the behavior a regression test exists to exercise; its prompt smoke check
passed. That prompt change loads on the next MCP server restart.

## Reproduce the passing configuration

From this worktree, with no other pytest job active:

```powershell
$env:PATH = 'C:/Program Files/Git/bin;' + $env:PATH
$env:PYTHONIOENCODING = 'utf-8'
& 'C:/Server/stacks/abs-kosync-enhanced/.venv/Scripts/python.exe' tests/run_all_tests.py -q --durations=40 --durations-min=0.5
```

Local raw logs, JUnit XML and cProfile files were preserved at
`C:/Users/cporc/.codex/worktrees/test-suite-profiling/evidence`: `serial`,
`parallel2`, `parallel2-clean`, `parallel4`, `culprits`,
`collection-culprits`, `gc-control`, `gc-stub`, and `provider-mock`.
`no-gc.log` records the aborted collection collision, not a passing measurement.

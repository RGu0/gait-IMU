# Replay Feeder Stability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ensure a high-speed replay finishes without repeatedly blocking on sub-schedulable delays.

**Architecture:** `ReplayDeviceSource` will preserve its timestamp ordering and normal paced replay.  It will treat a delay below a conservative scheduling threshold as immediate delivery, so a replay that is intentionally faster than the OS can schedule does not accumulate timer-quantisation waits.  The regression test freezes the presentation clock and proves the feeder completes and delivers all chunks.

**Tech Stack:** Python 3.13, `threading.Event`, pytest.

**Spec:** `D:\Onedrive\Project-Doc-Sync\gait-IMU\evidence\ray-545\requirement-revisions\R2.json`

## Global Constraints

- Scope: RAY-545 `replay-feeder-stability`, requirement revision R2.
- Do not address the failure by increasing its timeout.
- Preserve recorded ordering and existing normal-speed replay behavior.
- Verify through the governed test command on Windows Python 3.13.

## Review Focus

- A replay at normal speed still waits according to timestamps; own it in `test_synthetic_walk_produces_a_labelled_report`.
- Equal arrival timestamps retain recorded order; own it in `test_chunks_sharing_an_arrival_time_replay_in_recorded_order`.
- A frozen cosmetic clock does not cause high-speed feeder starvation; own it in the new regression test.
- An explicit stop still terminates a replay wait; covered by the existing `end_stream()` path.

---

### Task 1: Skip sub-schedulable replay waits

**Files:**
- Modify: `src/gait/app/replay.py:163-195`
- Modify: `tests/test_replay_source.py:137-150`

**Interfaces:**
- Consumes: `ReplayDeviceSource.speed`, `clock`, `_stop`, and the timestamped chunk timeline.
- Produces: a feeder that calls `Event.wait()` only for delays the host scheduler can meaningfully honour.

- [ ] **Step 1: Write the failing regression test**

Add `test_high_speed_replay_skips_sub_schedulable_waits` using several timestamped chunks, `speed=1e9`, and a frozen injected clock.  It must assert that the feeder completes within the existing 10-second helper and that both transports receive the original payload order.

- [ ] **Step 2: Run the focused test to verify it fails**

Run: governed `./dev test` with `tests/test_replay_source.py::test_high_speed_replay_skips_sub_schedulable_waits`.
Expected: FAIL because each sub-nanosecond delay is passed to `Event.wait()` and Windows rounds it into repeated scheduler waits.

- [ ] **Step 3: Implement a bounded scheduling threshold in `ReplayDeviceSource.begin_stream()`**

Define a private module constant for the minimum delay worth passing to `Event.wait()`.  For smaller positive delays, feed immediately; retain the existing stop check and current wait behavior at or above the threshold.

- [ ] **Step 4: Run the focused test to verify it passes**

Run: governed `./dev test` with `tests/test_replay_source.py::test_high_speed_replay_skips_sub_schedulable_waits`.
Expected: PASS with all payloads delivered in order.

- [ ] **Step 5: Run the replay module test file and commit**

Run: governed `./dev test` with `tests/test_replay_source.py`.
Expected: PASS.

Commit the source and test with a scoped RAY-545 message.

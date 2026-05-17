# THE PROGRESS FILE (progress.json):
#   Lives inside the dated experiment output directory.

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Data structures


# TrialSpec is one unit of work. The harness runs one TrialSpec at a time.
# trial_id is the canonical key used everywhere: e.g. "f1_condA_rep01"
@dataclass
class TrialSpec:
    trial_id: str  # e.g. "f1_condA_rep01" — unique key for this trial
    fault_id: str  # e.g. "f1"
    condition: str  # "A" or "B"
    repetition: int  # 1-based repetition number


# An excluded trial is one where the environment was not clean enough to run.
# e.g. baseline check failed — running the agent would produce garbage data.
@dataclass
class ExcludedEntry:
    trial_id: str
    reason: str  # human-readable reason, e.g. "baseline_check_failed"
    timestamp_utc: str  # when it was excluded


# A corrupted trial is one that was in_progress when the harness crashed,
# OR one where the agent run itself threw an unhandled exception.
# The trial is re-queued into pending so it runs again cleanly.
@dataclass
class CorruptedEntry:
    trial_id: str
    reason: str  # e.g. "in_progress_on_restart" or "agent_exception"
    timestamp_utc: str


# ProgressTracker — the main class


class ProgressTracker:
    """
    Manages progress.json for one experiment run.

    State machine for each trial:
      pending → in_progress → completed
                           → excluded
                           → corrupted → (re-queued into pending)
    """

    def __init__(
        self,
        output_dir: Path,
        experiment_id: str,
        completed: list[str],
        excluded: list[ExcludedEntry],
        corrupted: list[CorruptedEntry],
        in_progress: Optional[str],
        full_matrix: list[TrialSpec],
    ) -> None:
        # The path where progress.json lives.
        # Stored as an instance variable so every save() writes to the same place.
        self._path = output_dir / "progress.json"

        self.experiment_id = experiment_id
        self.completed: list[str] = completed
        self.excluded: list[ExcludedEntry] = excluded
        self.corrupted: list[CorruptedEntry] = corrupted
        self.in_progress: Optional[str] = in_progress

        # full_matrix is the complete list of ALL trials for this run.
        # It is never written to disk — it is always rebuilt from scratch
        # by build_trial_matrix() using the same FAULT_IDS and REPETITIONS.
        # We store it here only so pending can be computed by subtraction.
        self._full_matrix = full_matrix

    # Construction — two ways to get a ProgressTracker

    @classmethod
    def load_or_create(
        cls,
        output_dir: Path,
        full_matrix: list[TrialSpec],
    ) -> "ProgressTracker":
        """
        The only public constructor. Always use this — never call __init__ directly.

        If progress.json exists in output_dir → load it (resuming a run).
        If it does not exist              → create a fresh one (new run).

        In both cases, crash recovery runs automatically before returning.

        Args:
            output_dir:  The dated experiment directory. Must already exist.
            full_matrix: The complete list of TrialSpecs for this run,
                         built by build_trial_matrix(). Used to compute pending.
        """
        path = output_dir / "progress.json"

        if path.exists():
            logger.info(f"[progress] Resuming experiment from {path}")
            tracker = cls._load(path, full_matrix)
        else:
            logger.info(
                f"[progress] No progress file found — starting fresh at {path}"
            )
            tracker = cls._create_fresh(output_dir, full_matrix)

        #  Crash recovery
        # If in_progress is not None, the harness crashed while a trial
        # was running. We cannot trust any partial output from that trial.
        # Move it to corrupted and put it back in pending so it runs again.
        if tracker.in_progress is not None:
            crashed_id = tracker.in_progress
            logger.warning(
                f"[progress] Found in_progress='{crashed_id}' on startup. "
                f"Harness crashed mid-trial. Marking corrupted and re-queuing."
            )
            tracker.corrupted.append(
                CorruptedEntry(
                    trial_id=crashed_id,
                    reason="in_progress_on_restart",
                    timestamp_utc=_now_utc(),
                ))
            # Clear in_progress so it doesn't trigger recovery again next time.
            tracker.in_progress = None
            # Save immediately — recovery is not complete until it's on disk.
            tracker.save()

        # Log a summary so you can see where you stand at startup.
        pending_count = len(tracker.pending)
        logger.info(f"[progress] Status: "
                    f"{len(tracker.completed)} completed | "
                    f"{len(tracker.excluded)} excluded | "
                    f"{len(tracker.corrupted)} corrupted | "
                    f"{pending_count} pending")
        return tracker

    @classmethod
    def _create_fresh(
        cls,
        output_dir: Path,
        full_matrix: list[TrialSpec],
    ) -> "ProgressTracker":
        # experiment_id is a timestamp-based string.
        # It uniquely identifies this experiment run in every artifact file.
        experiment_id = f"exp_{datetime.now().strftime('%Y_%m_%d__%H_%M_%S')}"
        tracker = cls(
            output_dir=output_dir,
            experiment_id=experiment_id,
            completed=[],
            excluded=[],
            corrupted=[],
            in_progress=None,
            full_matrix=full_matrix,
        )
        # Write the file immediately so it exists on disk from the start.
        tracker.save()
        return tracker

    @classmethod
    def _load(
        cls,
        path: Path,
        full_matrix: list[TrialSpec],
    ) -> "ProgressTracker":
        raw = json.loads(path.read_text(encoding="utf-8"))

        return cls(
            output_dir=path.parent,
            experiment_id=raw["experiment_id"],
            completed=raw.get("completed", []),
            # Reconstruct dataclass instances from the plain dicts stored in JSON.
            # vars(entry) serialises a dataclass to a dict when saving.
            # ExcludedEntry(**dict) reconstructs it when loading.
            excluded=[ExcludedEntry(**e) for e in raw.get("excluded", [])],
            corrupted=[CorruptedEntry(**e) for e in raw.get("corrupted", [])],
            in_progress=raw.get("in_progress"),
            full_matrix=full_matrix,
        )

    # Derived state — pending is NEVER stored, always computed

    @property
    def pending(self) -> list[TrialSpec]:
        """
        Returns trials that still need to run, in the original matrix order.

        Computed by subtracting all finished trial_ids from the full matrix.
        'finished' means completed OR excluded OR corrupted — none of these
        should ever run again.

        THIS IS INTENTIONAL. A corrupted trial is not automatically re-run.
        It is flagged for human review. If you want to re-run it, remove its
        entry from the corrupted list in progress.json manually and restart.
        This is the safe choice — blind re-runs of corrupted trials could
        accumulate systematic errors silently.
        """
        done_ids = (set(self.completed)
                    | {e.trial_id
                       for e in self.excluded}
                    | {e.trial_id
                       for e in self.corrupted})
        # Return only trials whose trial_id is not in done_ids,
        # preserving the original matrix order.
        return [t for t in self._full_matrix if t.trial_id not in done_ids]

    # State transitions
    # Each method updates in-memory state AND saves to disk immediately.

    def set_in_progress(self, trial_id: str) -> None:
        """
        Called BEFORE starting a trial. Sets the crash sentinel.
        If the process dies after this call, recovery will find this
        trial_id and mark it corrupted on next startup.
        """
        self.in_progress = trial_id
        self.save()

    def clear_in_progress(self) -> None:
        """
        Called after a trial finishes (any outcome).
        Clears the crash sentinel.
        """
        self.in_progress = None
        self.save()

    def mark_completed(self, trial_id: str) -> None:
        """Trial ran successfully. Agent ran, artifact written."""
        if trial_id not in self.completed:
            self.completed.append(trial_id)
        self.in_progress = None
        self.save()

    def mark_excluded(self, trial_id: str, reason: str) -> None:
        """
        Trial skipped because the environment was not clean.
        e.g. baseline_check_failed — running the agent would produce bad data.
        Excluded trials do NOT re-run automatically.
        """
        self.excluded.append(
            ExcludedEntry(
                trial_id=trial_id,
                reason=reason,
                timestamp_utc=_now_utc(),
            ))
        self.in_progress = None
        self.save()

    def mark_corrupted(self, trial_id: str, reason: str) -> None:
        """
        Trial failed mid-execution (agent exception, K8s error, etc).
        Corrupted trials do NOT re-run automatically — manual review required.
        Remove from corrupted list in progress.json to force a re-run.
        """
        self.corrupted.append(
            CorruptedEntry(
                trial_id=trial_id,
                reason=reason,
                timestamp_utc=_now_utc(),
            ))
        self.in_progress = None
        self.save()

    # Persistence — atomic write

    def save(self) -> None:
        """
        Write progress state to disk atomically.

        HOW IT WORKS:
          1. Write the new content to a .tmp file next to progress.json.
          2. rename .tmp → progress.json in one atomic syscall.
          If the process dies between 1 and 2, the .tmp file is left behind
          but progress.json is still intact with the previous state.
          The orphaned .tmp file is harmless and will be overwritten next save.
        """
        doc = {
            "experiment_id": self.experiment_id,
            "total_trials": len(self._full_matrix),
            "last_updated_utc": _now_utc(),  # when was this file last written
            "completed": self.completed,
            "excluded": [vars(e) for e in self.excluded],
            "corrupted": [vars(e) for e in self.corrupted],
            "in_progress": self.in_progress,
        }

        # Write to a temp file first, then atomically rename.
        tmp_path = self._path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(doc, indent=2), encoding="utf-8")

        # This is the atomic step. On POSIX, rename is guaranteed to be
        # atomic — no other process can see a partial state.
        tmp_path.replace(self._path)

        logger.debug(f"[progress] Saved → {self._path}")


# Trial matrix builder


def build_trial_matrix(
    fault_ids: list[str],
    repetitions: int,
) -> list[TrialSpec]:
    """
    Builds the complete ordered list of TrialSpecs for the experiment.

    ORDER: rep → fault → condition (A before B).
    e.g. for 1 rep, faults [f1, f2]:
      f1_condA_rep01, f1_condB_rep01, f2_condA_rep01, f2_condB_rep01

    trial_id format: f1_condA_rep01
      - zero-padded rep (two digits) so alphabetic sort = chronological sort
    """
    matrix: list[TrialSpec] = []
    for rep in range(1, repetitions + 1):
        for fault_id in fault_ids:
            for condition in ["A", "B"]:
                trial_id = f"{fault_id}_cond{condition}_rep{rep:02d}"
                matrix.append(
                    TrialSpec(
                        trial_id=trial_id,
                        fault_id=fault_id,
                        condition=condition,
                        repetition=rep,
                    ))
    return matrix


# Helpers


def _now_utc() -> str:
    """Returns current UTC time as an ISO 8601 string. e.g. 2026-05-17T13:00:00Z"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

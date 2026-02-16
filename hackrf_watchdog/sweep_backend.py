"""
Watchdog sweep backend.

- Wraps `hackrf_sweep` and yields sweep frames (frequency/power bins).
- Supports one-shot sweeps (stable for time-slicing) and continuous streaming (stable for Windows USB).

tag=SB_V13BETA_STREAM_V1
"""

from __future__ import annotations

import os
import subprocess
import shutil
import time
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Tuple


class SweepBackendError(RuntimeError):
    pass


@dataclass
class SweepFrame:
    # center frequencies (Hz) and power (dB) for bins across the sweep
    freqs_hz: List[float]
    powers_db: List[float]
    # the raw range covered by the sweep
    start_hz: float
    stop_hz: float
    bin_width_hz: int


def _normalize_hackrf_serial(serial: str) -> str:
    """
    Normalize HackRF serials for hackrf_sweep's -d option.

    hackrf_info on Windows often prints 32 hex chars (16 bytes). hackrf_sweep commonly
    accepts the last 16 hex chars. We allow either, but prefer last 16 for consistency.
    """
    s = (serial or "").strip()
    if not s:
        return s
    # keep only hex
    s = "".join(ch for ch in s if ch.lower() in "0123456789abcdef")
    if len(s) > 16:
        s = s[-16:]
    return s


def _maybe_prepend_stdbuf(cmd: List[str]) -> List[str]:
    """Force line-buffering when stdout/stderr are piped.

    When hackrf_sweep stdout is captured (not a TTY), it can switch to block buffering
    and appear to "do nothing" for a long time (or indefinitely) from the UI's
    perspective. Wrapping with `stdbuf -oL -eL` forces line-buffering so we get data
    promptly.
    """

    # Only relevant on POSIX. On Windows, stdbuf is usually not available.
    if os.name != "posix":
        return cmd
    stdbuf = shutil.which("stdbuf")
    if not stdbuf:
        return cmd
    return [stdbuf, "-oL", "-eL", *cmd]


def _split_csv(line: str) -> Optional[List[str]]:
    line = line.strip()
    if not line or "," not in line:
        return None
    parts = [p.strip() for p in line.split(",")]
    # Expect: date, time, hz_low, hz_high, hz_bin_width, num_samples, dB, dB, ...
    if len(parts) < 7:
        return None
    return parts


def _parse_sweep_lines(lines: Sequence[str]) -> SweepFrame:
    """
    Parse hackrf_sweep CSV output lines into a SweepFrame.
    """
    freqs: List[float] = []
    pows: List[float] = []

    start_hz = None
    stop_hz = None
    bin_w = None

    for ln in lines:
        parts = _split_csv(ln)
        if not parts:
            continue

        try:
            hz_low = float(parts[2])
            hz_high = float(parts[3])
            bw = int(float(parts[4]))
        except Exception:
            continue

        # Some builds output multiple power columns; we take the max as the representative power
        db_vals = []
        for p in parts[6:]:
            try:
                db_vals.append(float(p))
            except Exception:
                pass
        if not db_vals:
            continue

        if start_hz is None or hz_low < start_hz:
            start_hz = hz_low
        if stop_hz is None or hz_high > stop_hz:
            stop_hz = hz_high
        if bin_w is None:
            bin_w = bw

        # Each line corresponds to a segment with multiple bins; compute bin centers.
        n = len(db_vals)
        span = hz_high - hz_low
        step = span / n if n > 0 else bw
        for i, db in enumerate(db_vals):
            # center of bin
            f = hz_low + (i + 0.5) * step
            freqs.append(f)
            pows.append(db)

    if start_hz is None or stop_hz is None or bin_w is None or not freqs:
        raise SweepBackendError("hackrf_sweep produced no parsable data")

    return SweepFrame(freqs_hz=freqs, powers_db=pows, start_hz=float(start_hz), stop_hz=float(stop_hz), bin_width_hz=int(bin_w))


def iter_sweep_frames(
    start_hz: float,
    stop_hz: float,
    bin_width_hz: int,
    *,
    extra_args: Optional[Sequence[str]] = None,
    freq_ppm: float = 0.0,
    continuous: bool = False,
) -> Iterable[SweepFrame]:
    """
    Yield SweepFrame objects.

    - If continuous=False (default): run hackrf_sweep in one-shot mode and yield one frame.
    - If continuous=True: run hackrf_sweep continuously and yield frames as they arrive.

    Notes:
    - On Windows, continuous mode is often much more stable than repeatedly starting -1 sweeps.
    """
    if start_hz <= 0 or stop_hz <= 0 or stop_hz <= start_hz:
        raise SweepBackendError(f"Invalid sweep range: {start_hz}..{stop_hz}")

    # hackrf_sweep expects MHz integers on some Windows builds (argument parser can be picky).
    start_mhz = int(round(start_hz / 1e6))
    stop_mhz = int(round(stop_hz / 1e6))

    args = list(extra_args or [])
    # Normalize -d serial if present
    if "-d" in args:
        try:
            di = args.index("-d")
            if di + 1 < len(args):
                args[di + 1] = _normalize_hackrf_serial(args[di + 1])
        except Exception:
            pass

    # Handle ppm correction: hackrf_sweep uses -p (ppm)?? It doesn't. We'll apply in parser later if needed.
    # (Leave here for future; keep signature stable.)

    # Ensure one-shot flag only in one-shot mode.
    if continuous:
        args = [a for a in args if a != "-1"]
    else:
        if "-1" not in args:
            args = ["-1"] + args

    cmd = ["hackrf_sweep", "-f", f"{start_mhz}:{stop_mhz}", "-w", str(int(bin_width_hz))] + args

    # On Windows, ensure hackrf tools folder is early in PATH. Caller usually sets it,
    # but we also respect existing PATH.
    env = os.environ.copy()

    exe_path = shutil.which("hackrf_sweep")
    cwd = None
    if exe_path:
        cmd[0] = exe_path
        cwd = os.path.dirname(exe_path)
        # Ensure the hackrf tools folder is early in PATH so required DLLs can be found
        env["PATH"] = cwd + os.pathsep + env.get("PATH", "")

    # Spawn process
    # Force line-buffering for hackrf_sweep when we capture its output.
    cmd = _maybe_prepend_stdbuf(cmd)

    # In continuous mode we merge stderr into stdout so we cannot deadlock on stderr
    # filling up while we only read stdout.
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=(subprocess.STDOUT if continuous else subprocess.PIPE),
        text=True,
        bufsize=1,
        universal_newlines=True,
        env=env,
        cwd=cwd,
    )

    def _die_with_err(prefix: str) -> None:
        """Try to include whatever output we can in the error."""
        try:
            out, err = proc.communicate(timeout=0.2)
        except Exception:
            out, err = "", ""

        # If stderr is merged into stdout, `err` will be empty; include a small stdout tail.
        tail = ""
        if (not err) and out:
            tail = out.strip()[-500:]
        if err:
            raise SweepBackendError(f"{prefix}. Exit code: {proc.returncode}, stderr: {err.strip()}")
        if tail:
            raise SweepBackendError(f"{prefix}. Exit code: {proc.returncode}, output: {tail}")
        raise SweepBackendError(f"{prefix}. Exit code: {proc.returncode}")

    try:
        if not continuous:
            out, err = proc.communicate()
            if proc.returncode != 0:
                raise SweepBackendError(f"hackrf_sweep failed. Exit code: {proc.returncode}, stderr: {err.strip()}")
            lines = [ln for ln in out.splitlines() if _split_csv(ln)]
            if not lines:
                raise SweepBackendError(f"hackrf_sweep produced no data. Exit code: {proc.returncode}, stderr: {err.strip()}")
            frame = _parse_sweep_lines(lines)
            # Apply ppm correction to freqs (simple scaling) if requested
            if freq_ppm:
                scale = 1.0 + (freq_ppm * 1e-6)
                frame.freqs_hz = [f * scale for f in frame.freqs_hz]
                frame.start_hz *= scale
                frame.stop_hz *= scale
            yield frame
            return

        # continuous streaming:
        current_key: Optional[Tuple[str, str]] = None
        buf: List[str] = []

        while True:
            if proc.stdout is None:
                _die_with_err("hackrf_sweep stdout not available")

            line = proc.stdout.readline()
            if line == "":
                # EOF
                rc = proc.poll()
                if rc is None:
                    time.sleep(0.01)
                    continue
                # process ended
                if buf:
                    frame = _parse_sweep_lines(buf)
                    if freq_ppm:
                        scale = 1.0 + (freq_ppm * 1e-6)
                        frame.freqs_hz = [f * scale for f in frame.freqs_hz]
                        frame.start_hz *= scale
                        frame.stop_hz *= scale
                    yield frame
                    buf.clear()
                if rc != 0:
                    _die_with_err("hackrf_sweep exited")
                return

            parts = _split_csv(line)
            if not parts:
                continue

            key = (parts[0], parts[1])  # date, time
            if current_key is None:
                current_key = key

            if key != current_key and buf:
                # Yield the completed sweep
                frame = _parse_sweep_lines(buf)
                if freq_ppm:
                    scale = 1.0 + (freq_ppm * 1e-6)
                    frame.freqs_hz = [f * scale for f in frame.freqs_hz]
                    frame.start_hz *= scale
                    frame.stop_hz *= scale
                yield frame
                buf = [line]
                current_key = key
            else:
                buf.append(line)

    finally:
        # Terminate if still running (important for continuous mode).
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.communicate(timeout=1.0)
                except Exception:
                    pass
        except Exception:
            pass

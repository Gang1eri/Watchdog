import sys
import time
import statistics
import math
import subprocess
import os
import shutil
import re
import ctypes
import ctypes.util
import inspect
from typing import List, Dict, Any, Optional

import traceback
import faulthandler
import datetime
from pathlib import Path as _Path

# ---------------------------------------------------------------------------
# Crash logging (writes watchdog_crash.log next to this script)
# ---------------------------------------------------------------------------
def _setup_crash_logging() -> None:
    try:
        log_dir = _Path(__file__).resolve().parent
    except Exception:
        log_dir = _Path.cwd()

    log_path = log_dir / "watchdog_crash.log"
    fh = None
    try:
        fh = open(log_path, "a", encoding="utf-8")
        fh.write("\n" + "=" * 80 + "\n")
        fh.write(f"Start: {datetime.datetime.now().isoformat()}\n")
        fh.flush()
        faulthandler.enable(file=fh, all_threads=True)
    except Exception:
        fh = None

    def _hook(exc_type, exc, tb):
        try:
            if fh:
                fh.write("\nUNCAUGHT EXCEPTION:\n")
                traceback.print_exception(exc_type, exc, tb, file=fh)
                fh.flush()
        except Exception:
            pass
        traceback.print_exception(exc_type, exc, tb)

    sys.excepthook = _hook

_setup_crash_logging()

from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtMultimedia import QSoundEffect

from hackrf_watchdog.sweep_backend import iter_sweep_frames, SweepBackendError
from hackrf_watchdog.atak_bridge import AtakBridge, AtakBridgeWindow



# ---------------------------------------------------------------------------
# WATCHDOG MULTI-SDR STEP1 (UI-only, no behavior change by default)
# ---------------------------------------------------------------------------
MODE_SINGLE = "Single SDR (current)"
MODE_PARALLEL = "Parallel SDRs (multi)"
DEVICE_HACKRF = "HackRF (hackrf_sweep)"
DEVICE_SOAPY = "SoapySDR (experimental)"


PERF_MAX_TRIPWIRE = "Max tripwire (Desktop)"
PERF_BALANCED = "Balanced"
PERF_CPU_LITE = "CPU-Lite (RPi/DragonOS)"
PERF_CUSTOM = "Custom"
PERF_PRESETS = (PERF_MAX_TRIPWIRE, PERF_BALANCED, PERF_CPU_LITE, PERF_CUSTOM)


# ---------------------------------------------------------------------------
# Bias-T / antenna power control helper
# ---------------------------------------------------------------------------

def set_bias_tee(enable: bool, log_fn, serial: Optional[str] = None) -> bool:
    exe = shutil.which("hackrf_biast") or shutil.which("hackrf_biast.exe")
    if not exe:
        return False

    mode = "1" if enable else "0"
    cmd = [exe, "-b", mode, "-r", ("on" if enable else "off")]
    if serial:
        cmd += ["-d", str(serial)]

    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=2)
        log_fn(f"Bias-T set to: {'ON' if enable else 'OFF'} (via hackrf_biast)")
        return True
    except Exception as e:
        log_fn(f"Bias-T command failed (hackrf_biast): {e}")
        return False


# ---------------------------------------------------------------------------
# HackRF device detection
# ---------------------------------------------------------------------------

def list_hackrf_devices() -> List[Dict[str, str]]:
    devices: List[Dict[str, str]] = []
    try:
        result = subprocess.run(
            ["hackrf_info"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=5,
        )
    except Exception:
        return devices

    index = -1
    for line in result.stdout.splitlines():
        raw = line.strip()
        low = raw.lower()

        if low.startswith("found hackrf"):
            index += 1

        if "serial" in low and ":" in raw:
            _, val = raw.split(":", 1)
            serial = val.strip()
            if serial:
                devices.append({"index": str(index), "serial": serial})

    return devices



# ---------------------------------------------------------------------------
# SoapySDR device detection (optional)
# ---------------------------------------------------------------------------

def list_soapy_devices() -> List[Dict[str, Any]]:
    """Return discovered SoapySDR devices.

    Windows-friendly: does NOT require Python SoapySDR bindings.
    Uses SoapySDRUtil (PothosSDR) and parses its output.

    Each item contains:
      - label: display label
      - args_str: selector string like 'driver=bladerf,serial=...'
      - info: parsed key/value dict
    """
    util = shutil.which("SoapySDRUtil")

    if util is None and os.name == "nt":
        roots: List[str] = []
        env_root = os.environ.get("POTHOS")
        if env_root:
            roots.append(env_root)
        roots.extend([r"C:\Program Files\PothosSDR", r"C:\Program Files (x86)\PothosSDR"])
        for root in roots:
            cand = os.path.join(root, "bin", "SoapySDRUtil.exe")
            if os.path.isfile(cand):
                util = cand
                break

    if not util:
        return []

    try:
        p = subprocess.run(
            [util, "--find"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        out = p.stdout or ""
    except Exception:
        return []

    # Parse SoapySDRUtil output blocks.
    blocks: List[Dict[str, str]] = []
    cur: Optional[Dict[str, str]] = None
    for raw in out.splitlines():
        line = raw.rstrip("\r\n")
        if line.startswith("Found device"):
            if cur is not None:
                blocks.append(cur)
            cur = {}
            continue
        if cur is None:
            continue
        m = re.match(r"^\s*([A-Za-z0-9_]+)\s*=\s*(.*)\s*$", line)
        if not m:
            continue
        cur[m.group(1).strip()] = m.group(2).strip()
    if cur is not None:
        blocks.append(cur)

    devices: List[Dict[str, Any]] = []
    for info in blocks:
        driver = str(info.get("driver") or "").strip()
        if not driver:
            continue
        dlow = driver.lower()
        serial = str(info.get("serial") or "").strip()
        label = str(info.get("label") or "").strip() or driver
        # Hide Soapy-provided "hackrf" entries. We always use native HackRF via hackrf_sweep.
        # Also hide audio devices.
        label_l = (label or "").lower()
        if dlow == "audio" or "audio" in dlow:
            continue
        if "hackrf" in dlow or "hackrf" in label_l:
            continue

        args = f"driver={driver}"
        if serial:
            args += f",serial={serial}"

        devices.append({"label": label, "args_str": args, "info": info})

    return devices

# ---------------------------------------------------------------------------
# SoapySDR C-API wrapper (ctypes)
#   - Avoids requiring the python "SoapySDR" module.
#   - Uses the SoapySDR runtime (SoapySDR.dll / libSoapySDR.so) installed by
#     PothosSDR or your distro packages.
# ---------------------------------------------------------------------------

class _SoapyCAPI:
    """
    Minimal ctypes wrapper for the SoapySDR C API needed for RX time-slicing.

    This is intentionally small:
      - makeStrArgs / unmake
      - setFrequency / setSampleRate / setBandwidth / setGain
      - setupStream / activate / read / deactivate / close
      - lastError (for diagnostics)

    Requires SoapySDR runtime (e.g., PothosSDR on Windows).
    """
    SOAPY_SDR_RX = 1  # per SoapySDR constants: RX direction is 1

    def __init__(self):
        self.lib = self._load_library()
        self._bind()

    @staticmethod
    def _load_library():
        if os.name == "nt":
            # Windows: try to locate SoapySDR.dll without requiring the user to edit PATH.
            # Typical PothosSDR layout: <root>\bin\SoapySDR.dll
            roots: List[str] = []
            env_root = os.environ.get("POTHOS")
            if env_root:
                roots.append(env_root)
            roots.extend([r"C:\Program Files\PothosSDR", r"C:\Program Files (x86)\PothosSDR"])

            # If the DLL is found, add its directory to the DLL search path (Python 3.8+)
            for root in roots:
                dll_path = os.path.join(root, "bin", "SoapySDR.dll")
                if os.path.isfile(dll_path):
                    try:
                        os.add_dll_directory(os.path.dirname(dll_path))
                    except Exception:
                        # Older Python/edge cases: fall back to PATH prepend
                        os.environ["PATH"] = os.path.dirname(dll_path) + os.pathsep + os.environ.get("PATH", "")
                    return ctypes.WinDLL(dll_path)

            # Fall back to name-based load (works if user/launcher already set PATH)
            return ctypes.WinDLL("SoapySDR.dll")
        # Linux/macOS
        name = ctypes.util.find_library("SoapySDR")
        if name:
            return ctypes.CDLL(name)
        # Linux/macOS
        name = ctypes.util.find_library("SoapySDR")
        if name:
            return ctypes.CDLL(name)
        # Fallback common sonames
        for cand in ("libSoapySDR.so", "libSoapySDR.so.0.8", "libSoapySDR.so.0.8.1"):
            try:
                return ctypes.CDLL(cand)
            except OSError:
                pass
        raise OSError("Could not locate the SoapySDR shared library")

    def _bind(self):
        lib = self.lib
        c_void_p = ctypes.c_void_p
        c_size_t = ctypes.c_size_t
        c_int = ctypes.c_int
        c_longlong = ctypes.c_longlong
        c_long = ctypes.c_long
        c_double = ctypes.c_double
        c_char_p = ctypes.c_char_p

        # Error helpers
        self._lastError = lib.SoapySDRDevice_lastError
        self._lastError.restype = c_char_p
        self._lastError.argtypes = []

        # Device lifetime
        self._makeStrArgs = lib.SoapySDRDevice_makeStrArgs
        self._makeStrArgs.restype = c_void_p
        self._makeStrArgs.argtypes = [c_char_p]

        self._unmake = lib.SoapySDRDevice_unmake
        self._unmake.restype = c_int
        self._unmake.argtypes = [c_void_p]

        # Tuning & gains
        self._setSampleRate = lib.SoapySDRDevice_setSampleRate
        self._setSampleRate.restype = c_int
        self._setSampleRate.argtypes = [c_void_p, c_int, c_size_t, c_double]

        self._setBandwidth = lib.SoapySDRDevice_setBandwidth
        self._setBandwidth.restype = c_int
        self._setBandwidth.argtypes = [c_void_p, c_int, c_size_t, c_double]

        self._setFrequency = lib.SoapySDRDevice_setFrequency
        self._setFrequency.restype = c_int
        # (device, direction, channel, frequency, args)
        self._setFrequency.argtypes = [c_void_p, c_int, c_size_t, c_double, c_void_p]

        self._setGain = lib.SoapySDRDevice_setGain
        self._setGain.restype = c_int
        self._setGain.argtypes = [c_void_p, c_int, c_size_t, c_double]

        # Stream API
        self._setupStream = lib.SoapySDRDevice_setupStream
        self._setupStream.restype = c_void_p
        self._setupStream.argtypes = [c_void_p, c_int, c_char_p, ctypes.POINTER(c_size_t), c_size_t, c_void_p]

        self._activateStream = lib.SoapySDRDevice_activateStream
        self._activateStream.restype = c_int
        self._activateStream.argtypes = [c_void_p, c_void_p, c_int, c_longlong, c_size_t]

        self._deactivateStream = lib.SoapySDRDevice_deactivateStream
        self._deactivateStream.restype = c_int
        self._deactivateStream.argtypes = [c_void_p, c_void_p, c_int, c_longlong]

        self._closeStream = lib.SoapySDRDevice_closeStream
        self._closeStream.restype = c_int
        self._closeStream.argtypes = [c_void_p, c_void_p]

        self._readStream = lib.SoapySDRDevice_readStream
        self._readStream.restype = c_int
        self._readStream.argtypes = [
            c_void_p,                # device
            c_void_p,                # stream
            ctypes.POINTER(c_void_p),# buffs
            c_size_t,                # numElems
            ctypes.POINTER(c_int),   # flags
            ctypes.POINTER(c_longlong), # timeNs
            c_long,                  # timeoutUs
        ]

    def last_error(self) -> str:
        try:
            s = self._lastError()
            if not s:
                return ""
            return s.decode("utf-8", errors="ignore")
        except Exception:
            return ""

    def make(self, args_str: str):
        dev = self._makeStrArgs(args_str.encode("utf-8"))
        if not dev:
            raise RuntimeError(f"SoapySDRDevice_makeStrArgs failed for '{args_str}'. {self.last_error()}")
        return dev

    def unmake(self, dev):
        if dev:
            self._unmake(dev)

    def set_sample_rate(self, dev, direction: int, chan: int, rate_hz: float):
        self._setSampleRate(dev, int(direction), ctypes.c_size_t(chan), float(rate_hz))

    def set_bandwidth(self, dev, direction: int, chan: int, bw_hz: float):
        self._setBandwidth(dev, int(direction), ctypes.c_size_t(chan), float(bw_hz))

    def set_frequency(self, dev, direction: int, chan: int, freq_hz: float):
        # args = NULL
        self._setFrequency(dev, int(direction), ctypes.c_size_t(chan), float(freq_hz), ctypes.c_void_p(0))

    def set_gain(self, dev, direction: int, chan: int, gain_db: float):
        self._setGain(dev, int(direction), ctypes.c_size_t(chan), float(gain_db))

    def setup_stream_cf32(self, dev, direction: int, chan: int):
        chans = (ctypes.c_size_t * 1)(ctypes.c_size_t(chan))
        stream = self._setupStream(dev, int(direction), b"CF32", chans, ctypes.c_size_t(1), ctypes.c_void_p(0))
        if not stream:
            raise RuntimeError(f"SoapySDRDevice_setupStream failed. {self.last_error()}")
        return stream

    def activate_stream(self, dev, stream):
        ret = self._activateStream(dev, stream, 0, 0, 0)
        if ret < 0:
            raise RuntimeError(f"SoapySDRDevice_activateStream failed ({ret}). {self.last_error()}")

    def deactivate_stream(self, dev, stream):
        self._deactivateStream(dev, stream, 0, 0)

    def close_stream(self, dev, stream):
        self._closeStream(dev, stream)

    def read_stream(self, dev, stream, buff_ptr, num_elems: int, timeout_us: int = 100000) -> int:
        flags = ctypes.c_int(0)
        timeNs = ctypes.c_longlong(0)
        ret = self._readStream(dev, stream, buff_ptr, ctypes.c_size_t(num_elems), ctypes.byref(flags), ctypes.byref(timeNs), ctypes.c_long(int(timeout_us)))
        return int(ret)


_SOAPY_CAPI_SINGLETON: Optional[_SoapyCAPI] = None

def soapy_capi_available() -> bool:
    global _SOAPY_CAPI_SINGLETON
    if _SOAPY_CAPI_SINGLETON is not None:
        return True
    try:
        _SOAPY_CAPI_SINGLETON = _SoapyCAPI()
        return True
    except Exception:
        _SOAPY_CAPI_SINGLETON = None
        return False

def get_soapy_capi() -> _SoapyCAPI:
    global _SOAPY_CAPI_SINGLETON
    if _SOAPY_CAPI_SINGLETON is None:
        _SOAPY_CAPI_SINGLETON = _SoapyCAPI()
    return _SOAPY_CAPI_SINGLETON
class SweepWorker(QtCore.QObject):
    log_message = QtCore.pyqtSignal(str)
    noise_floor_updated = QtCore.pyqtSignal(float)
    detections_found = QtCore.pyqtSignal(list)
    finished = QtCore.pyqtSignal()

    def __init__(
        self,
        bands: List[Dict[str, Any]],
        bin_width_hz: int,
        threshold_db: float,
        use_local_noise_floor: bool,
        only_above_threshold: bool,
        min_hold_time_s: float,
        interval_ms: int,
        start_delay_ms: int = 0,
        device_arg: Optional[str] = None,
        antenna_power: bool = False,
        cal_gain_db: float = 0.0,
        cal_loss_db: float = 0.0,
        freq_ppm: float = 0.0,
        source_id: str = "",
        parent=None,
    ):
        super().__init__(parent)
        self.bands = bands
        self.bin_width_hz = bin_width_hz
        self.threshold_db = float(threshold_db)
        self.use_local_noise_floor = bool(use_local_noise_floor)
        self.only_above_threshold = bool(only_above_threshold)
        self.min_hold_time_s = float(min_hold_time_s)
        self.interval_ms = int(interval_ms)
        self.start_delay_ms = int(start_delay_ms)
        self.start_delay_s = self.start_delay_ms / 1000.0
        self.device_arg = device_arg

        self.antenna_power = bool(antenna_power)
        self.cal_gain_db = float(cal_gain_db)
        self.cal_loss_db = float(cal_loss_db)
        self.freq_ppm = float(freq_ppm)
        self.source_id = str(source_id)

        # Stable label for logging/UI (prevents AttributeError if used before set elsewhere)
        self.device_label = (str(source_id).strip() or str(device_arg).strip() or "SDR")

        self._running = True
        self._transient_failures = 0
        self._post_sweep_delay_s = 0.20
        try:
            self._supports_continuous = "continuous" in inspect.signature(iter_sweep_frames).parameters
        except Exception:
            self._supports_continuous = False
        self._noise_floor = None
        self._hold_state: Dict[float, Dict[str, Any]] = {}
        # Throttle 'Max:' log lines to keep UI responsive (engine may run very fast)
        self._last_max_log_t = 0.0
        self.max_log_period_s = 0.25

    @QtCore.pyqtSlot()
    def run(self):
        try:
            if self.start_delay_s > 0:
                self.log_message.emit(f"Worker start delay: {self.start_delay_ms} ms")
                self._sleep_interruptible(self.start_delay_s)

            while self._running:
                enabled_bands = [b for b in self.bands if b.get("enabled")]
                use_continuous_hackrf = bool(self.device_arg) and len(enabled_bands) == 1

                # Interval behavior:
                #   - Continuous HackRF mode (single band per HackRF worker): hackrf_sweep stays open.
                #     Interval is used only as a *processing/UI throttle* so we don't spam the UI/log.
                #   - One-shot mode: hackrf_sweep is relaunched each sweep; too-small intervals can
                #     cause USB/device-open flakiness on Windows, so we enforce a conservative minimum.
                effective_interval_ms = int(self.interval_ms)

                # One-time notice per worker
                if not hasattr(self, "_interval_notice_shown"):
                    self._interval_notice_shown = False

                if use_continuous_hackrf:
                    # Continuous HackRF mode: keep hackrf_sweep running and process frames as fast as possible
                    # (interval_ms=0 means "max"). Any UI smoothing is handled in the main thread.
                    if effective_interval_ms < 0:
                        effective_interval_ms = 0
                else:
                    # One-shot mode: hackrf_sweep is relaunched each sweep. Too-small intervals can cause
                    # USB/device-open flakiness (especially on Windows), so enforce a conservative minimum.
                    if effective_interval_ms < 750:
                        if not self._interval_notice_shown:
                            self.log_message.emit(
                                f"Interval raised to 750 ms for HackRF stability in one-shot mode (was {effective_interval_ms} ms)."
                            )
                            self._interval_notice_shown = True
                        effective_interval_ms = 750

                cycle_start = time.time()
                any_band = False

                for band in self.bands:
                    if not self._running:
                        break
                    if not band.get("enabled"):
                        continue

                    any_band = True
                    start_hz = band["start_hz"]
                    stop_hz = band["stop_hz"]

                    try:
                        extra_args = []
                        if not use_continuous_hackrf:
                            extra_args.append("-1")
                        if self.device_arg:
                            extra_args += ["-d", self.device_arg]

                        if self.antenna_power:
                            extra_args += ["-p", "1"]

                        if use_continuous_hackrf and not self._supports_continuous:
                            self.log_message.emit(
                                "Warning: sweep_backend.py does not support continuous mode; "
                                "falling back to one-shot sweeps. (Update sweep_backend.py for best stability.)"
                            )
                            use_continuous_hackrf = False
                            if "-1" not in extra_args:
                                extra_args.insert(0, "-1")

                        kwargs = dict(extra_args=extra_args, freq_ppm=self.freq_ppm)
                        if self._supports_continuous:
                            kwargs["continuous"] = use_continuous_hackrf

                        # In continuous mode we must keep draining stdout to avoid pipe back-pressure.
                        # So we do NOT sleep between frames; instead we optionally skip processing
                        # until the interval has elapsed.
                        next_allowed = 0.0


                        # Sweep-rate stats (raw vs processed). We report occasionally so you can
                        # see how fast hackrf_sweep is actually producing sweeps, vs how often we
                        # choose to process/update the UI.
                        rate_t0 = time.monotonic()
                        raw_frames = 0
                        processed_frames = 0
                        skipped_frames = 0
                        last_rate_log = rate_t0
                        RATE_LOG_PERIOD_S = 5.0
                        for frame in iter_sweep_frames(
                            start_hz,
                            stop_hz,
                            self.bin_width_hz,
                            **kwargs,
                        ):
                            if not self._running:
                                break


                            raw_frames += 1
                            now_m = time.monotonic()
                            # Periodic rate log even if we're skipping processing
                            if (now_m - last_rate_log) >= RATE_LOG_PERIOD_S:
                                dt = max(1e-6, now_m - rate_t0)
                                raw_rate = raw_frames / dt
                                proc_rate = processed_frames / dt
                                skip_rate = skipped_frames / dt
                                self.log_message.emit(
                                    f"[{self.device_label}] Sweep rate: raw={raw_rate:.1f}/s, processed={proc_rate:.1f}/s, skipped={skip_rate:.1f}/s (interval={effective_interval_ms} ms, continuous={use_continuous_hackrf})"
                                )
                                rate_t0 = now_m
                                last_rate_log = now_m
                                raw_frames = 0
                                processed_frames = 0
                                skipped_frames = 0
                            if use_continuous_hackrf and effective_interval_ms > 0:
                                now = time.monotonic()
                                if now < next_allowed:
                                    skipped_frames += 1
                                    continue
                                next_allowed = now + (effective_interval_ms / 1000.0)

                            self._handle_frame(band, frame)

                            processed_frames += 1
                            # One-shot mode: optional delay between launches.
                            if (not use_continuous_hackrf) and effective_interval_ms > 0:
                                self._sleep_interruptible(effective_interval_ms / 1000.0)

                        self._transient_failures = 0
                        if not use_continuous_hackrf:
                            time.sleep(self._post_sweep_delay_s)

                    except SweepBackendError as e:
                        # If we're stopping, don't spam retries/logs; just exit cleanly.
                        if not self._running:
                            break
                        msg = str(e)
                        # Treat some HackRF errors as transient (USB hiccups / rapid re-open)
                        if self._is_transient_backend_error(msg):
                            self._transient_failures += 1
                            delay = min(5.0, 0.5 * (2 ** min(self._transient_failures, 4)))
                            self.log_message.emit(
                                f"Transient HackRF error; retrying in {delay:.1f}s. Details: {msg}"
                            )
                            self._sleep_interruptible(delay)
                            if not self._running:
                                break
                            continue
                        else:
                            self.log_message.emit(f"Error from hackrf_sweep: {msg}")
                            self._sleep_interruptible(1.0)
                            if not self._running:
                                break
                            continue

                    except Exception as e:
                        if not self._running:
                            break
                        import traceback
                        self.log_message.emit("Unexpected sweep worker error:\n" + traceback.format_exc())
                        self._sleep_interruptible(1.0)
                        if not self._running:
                            break

                if not self._running:
                    break

                if not any_band:
                    self.log_message.emit("No bands enabled; worker sleeping.")
                    time.sleep(1.0)

                if self.interval_ms > 0:
                    elapsed_ms = (time.time() - cycle_start) * 1000.0
                    remaining = self.interval_ms - elapsed_ms
                    if remaining > 0:
                        time.sleep(remaining / 1000.0)
        finally:
            self.finished.emit()

    def stop(self):
        self._running = False

    def _sleep_interruptible(self, seconds: float):
        """Sleep in small chunks so Stop can interrupt quickly."""
        end = time.time() + max(0.0, float(seconds))
        while self._running and time.time() < end:
            time.sleep(min(0.1, end - time.time()))


    def _is_transient_backend_error(self, msg: str) -> bool:
        m = (msg or "").lower()
        # Common Windows/libusb transient failures when rapidly opening devices
        if "hackrf_open() failed" in m and "not found" in m and "(-5" in m:
            return True
        if "device or resource busy" in m or "resource busy" in m:
            return True
        if "libusb" in m and "error" in m and "busy" in m:
            return True
        return False

    def _net_cal_offset_db(self) -> float:
        return float(self.cal_gain_db) - float(self.cal_loss_db)

    def _freq_factor(self) -> float:
        return 1.0 + (float(self.freq_ppm) / 1e6)

    def _handle_frame(self, band: Dict[str, Any], frame: Any) -> None:
        """Process one sweep frame.

        `sweep_backend.iter_sweep_frames()` may return either the legacy dict-frame
        shape (keys like `powers_dbm`, `low_hz`, `bin_width_hz`) OR a SweepFrame
        dataclass (attributes like `powers_db`, `start_hz`, `bin_width_hz`).
        """

        def _fg(obj: Any, key: str, default=None):
            if isinstance(obj, dict):
                return obj.get(key, default)
            return getattr(obj, key, default)

        powers_raw = _fg(frame, "powers_dbm")
        if powers_raw is None:
            powers_raw = _fg(frame, "powers_db")
        if powers_raw is None:
            powers_raw = _fg(frame, "powers")
        if not powers_raw:
            return

        cal_offset = self._net_cal_offset_db()
        powers = [float(p) + float(cal_offset) for p in powers_raw]

        sorted_p = sorted(powers)
        if len(sorted_p) > 10:
            cutoff = int(len(sorted_p) * 0.8)
            noise_candidates = sorted_p[:cutoff]
        else:
            noise_candidates = sorted_p
        median_noise = statistics.median(noise_candidates)

        if self._noise_floor is None:
            self._noise_floor = median_noise
        else:
            alpha = 0.1
            self._noise_floor = (1 - alpha) * self._noise_floor + alpha * median_noise

        self.noise_floor_updated.emit(self._noise_floor)

        if self.use_local_noise_floor:
            abs_threshold = self._noise_floor + float(self.threshold_db)
        else:
            abs_threshold = float(self.threshold_db)

        # Prefer explicit low/bin info (hackrf_sweep legacy), otherwise fall back
        # to SweepFrame-style attributes.
        low_hz = _fg(frame, "low_hz")
        if low_hz is None:
            low_hz = _fg(frame, "start_hz")
        bin_w = _fg(frame, "bin_width_hz")
        if bin_w is None:
            bin_w = _fg(frame, "bin_width")

        freqs_hz = _fg(frame, "freqs_hz")
        if freqs_hz is not None and (low_hz is None or bin_w is None):
            # Infer a low edge + bin width from center frequencies.
            try:
                freqs_hz_list = list(freqs_hz)
                if len(freqs_hz_list) >= 2:
                    inferred_bin = float(freqs_hz_list[1]) - float(freqs_hz_list[0])
                    if bin_w is None:
                        bin_w = inferred_bin
                    if low_hz is None:
                        low_hz = float(freqs_hz_list[0]) - 0.5 * float(bin_w)
            except Exception:
                pass

        if low_hz is None or bin_w is None:
            # If we still can't determine these, we can't map index->frequency.
            return

        low_hz = float(low_hz)
        bin_w = float(bin_w)
        f_factor = self._freq_factor()

        detections: List[Dict[str, Any]] = []
        max_power = None
        max_freq_mhz = None

        now = time.time()
        hold = float(self.min_hold_time_s)

        band_start_hz = float(band.get("start_hz", float("-inf")))
        band_stop_hz = float(band.get("stop_hz", float("inf")))

        n_bins = len(powers)
        for idx in range(n_bins):
            p_cal = powers[idx]
            p_raw = powers_raw[idx]

            center_hz_raw = low_hz + (idx + 0.5) * bin_w
            # Guard: some sweep backends may yield bins slightly outside the requested range.
            # Only consider bins that fall within the band limits.
            if center_hz_raw < band_start_hz or center_hz_raw > band_stop_hz:
                continue
            center_hz = center_hz_raw * f_factor

            freq_mhz_raw = center_hz_raw / 1e6
            freq_mhz = center_hz / 1e6

            key = round(freq_mhz, 6)
            st = self._hold_state.get(key)

            if p_cal >= abs_threshold:
                if st is None or not st.get("above", False):
                    st = {"first_seen": now, "last_seen": now, "above": True}
                    self._hold_state[key] = st
                else:
                    st["last_seen"] = now
                    st["above"] = True

                dwell = 0.0 if st["first_seen"] is None else now - st["first_seen"]

                if hold <= 0 or dwell >= hold:
                    detections.append(
                        {
                            "freq_mhz": freq_mhz,
                            "freq_mhz_raw": freq_mhz_raw,
                            "power_dbm": p_cal,
                            "power_dbm_raw": p_raw,
                            "cal_offset_db": cal_offset,
                            "freq_ppm": float(self.freq_ppm),
                            "timestamp": now,
                            "band": band.get("name", ""),
                            "source": self.source_id,
                        }
                    )
            else:
                if st is not None and st.get("above", False):
                    st["above"] = False
                    st["first_seen"] = None
                    st["last_seen"] = now

            if max_power is None or p_cal > max_power:
                max_power = p_cal
                max_freq_mhz = freq_mhz

        cleanup_limit = max(hold * 2.0, 10.0)
        stale_keys = []
        for k, st in self._hold_state.items():
            last_seen = st.get("last_seen")
            if last_seen is not None and (now - last_seen) > cleanup_limit:
                stale_keys.append(k)
        for k in stale_keys:
            del self._hold_state[k]

        span_txt = f"{band['start_mhz']:.3f}-{band['stop_mhz']:.3f} MHz"
        if max_power is not None and max_freq_mhz is not None:

            line = f"Max: {max_power:.1f} dB at {max_freq_mhz:.6f} MHz (span {span_txt})"

            # Throttle Max logging; detections are still emitted immediately.
            now_t = time.time()
            if (now_t - getattr(self, "_last_max_log_t", 0.0)) >= float(getattr(self, "max_log_period_s", 0.25)):
                self._last_max_log_t = now_t
                if self.only_above_threshold:
                    if max_power >= abs_threshold:
                        self.log_message.emit(line)
                else:
                    self.log_message.emit(line)

        if detections:
            self.detections_found.emit(detections)



# ---------------------------------------------------------------------------
# SoapySDR time-slice worker (bladeRF via SoapySDR)
# ---------------------------------------------------------------------------

class SoapyTimeSliceWorker(QtCore.QObject):
    """Time-slices one SDR across multiple bands (retune -> dwell -> FFT -> bins -> detections).

    Notes:
      - Power units are *relative* (FFT power in dB). Detection works well in
        'Use local noise floor' mode.
      - This worker intentionally mirrors SweepWorker's signal/slot API.
    """

    log_message = QtCore.pyqtSignal(str)
    noise_floor_updated = QtCore.pyqtSignal(float)
    detections_found = QtCore.pyqtSignal(list)
    finished = QtCore.pyqtSignal()

    def __init__(
        self,
        bands: List[Dict[str, Any]],
        bin_width_hz: int,
        threshold_db: float,
        use_local_noise_floor: bool,
        only_above_threshold: bool,
        min_hold_time_s: float,
        interval_ms: int,
        soapy_args: str,
        sample_rate_hz: float = 20e6,
        bandwidth_hz: float = 20e6,
        gain_db: float = 30.0,
        dwell_ms: int = 250,
        settle_ms: int = 40,
        fft_size: int = 4096,
        avg_frames: int = 4,
        cal_gain_db: float = 0.0,
        cal_loss_db: float = 0.0,
        freq_ppm: float = 0.0,
        source_id: str = "Soapy",
        parent=None,
    ):
        super().__init__(parent)
        self.bands = bands
        self.bin_width_hz = int(bin_width_hz)
        self.threshold_db = float(threshold_db)
        self.use_local_noise_floor = bool(use_local_noise_floor)
        self.only_above_threshold = bool(only_above_threshold)
        self.min_hold_time_s = float(min_hold_time_s)
        self.interval_ms = int(interval_ms)

        self.soapy_args = str(soapy_args or "driver=bladerf")
        self.sample_rate_hz = float(sample_rate_hz)
        self.bandwidth_hz = float(bandwidth_hz)
        self.gain_db = float(gain_db)
        self.dwell_ms = int(dwell_ms)
        self.settle_ms = int(settle_ms)
        self.fft_size = int(fft_size)
        self.avg_frames = int(avg_frames)

        self.cal_gain_db = float(cal_gain_db)
        self.cal_loss_db = float(cal_loss_db)
        self.freq_ppm = float(freq_ppm)
        self.source_id = str(source_id)

        self._running = True
        self._noise_floor = None
        self._hold_state: Dict[float, Dict[str, Any]] = {}

        self._np = None
        self._SOAPY_SDR_RX = None
        self._dev = None
        self._rx_stream = None
        self._chan = 0

    def stop(self):
        self._running = False

    def _net_cal_offset_db(self) -> float:
        return float(self.cal_gain_db) - float(self.cal_loss_db)

    def _freq_factor(self) -> float:
        return 1.0 + (float(self.freq_ppm) / 1e6)

    def _parse_soapy_args(self, s: str) -> Dict[str, str]:
        out: Dict[str, str] = {}
        for part in (s or "").split(","):
            part = part.strip()
            if not part:
                continue
            if "=" in part:
                k, v = part.split("=", 1)
                out[k.strip()] = v.strip()
        if not out:
            out = {"driver": "bladerf"}
        return out

    def _ensure_device(self) -> None:
        if self._dev is not None:
            return

        try:
            import numpy as np  # type: ignore
        except Exception as e:
            raise RuntimeError(f"Soapy time-slice requires numpy. Import error: {e}")

        self._np = np

        if not soapy_capi_available():
            raise RuntimeError(
                "SoapySDR runtime not available. Install PothosSDR (recommended on Windows) "
                "and ensure SoapySDR.dll is on PATH (PothosSDR\\bin)."
            )

        self._soapy = get_soapy_capi()
        self._SOAPY_SDR_RX = self._soapy.SOAPY_SDR_RX

        # Open device from argument string like: 'driver=bladerf,serial=...'
        self._dev = self._soapy.make(self.soapy_args)

        # Configure
        self._soapy.set_sample_rate(self._dev, self._SOAPY_SDR_RX, self._chan, self.sample_rate_hz)
        self._soapy.set_bandwidth(self._dev, self._SOAPY_SDR_RX, self._chan, self.bandwidth_hz)
        try:
            self._soapy.set_gain(self._dev, self._SOAPY_SDR_RX, self._chan, self.gain_db)
        except Exception:
            pass

        # Stream (CF32 complex float)
        self._rx_stream = self._soapy.setup_stream_cf32(self._dev, self._SOAPY_SDR_RX, self._chan)
        self._soapy.activate_stream(self._dev, self._rx_stream)

        self.log_message.emit(
            f"SoapySDR ready: {self.soapy_args} (Fs={self.sample_rate_hz/1e6:.1f} MHz, BW={self.bandwidth_hz/1e6:.1f} MHz)"
        )
    def _close_device(self) -> None:
        soapy = getattr(self, "_soapy", None)
        try:
            if soapy is not None and self._dev is not None and self._rx_stream is not None:
                try:
                    soapy.deactivate_stream(self._dev, self._rx_stream)
                except Exception:
                    pass
                try:
                    soapy.close_stream(self._dev, self._rx_stream)
                except Exception:
                    pass
        finally:
            try:
                if soapy is not None and self._dev is not None:
                    soapy.unmake(self._dev)
            except Exception:
                pass
            self._rx_stream = None
            self._dev = None
    def _set_center_freq(self, center_hz: float) -> None:
        f_factor = self._freq_factor()
        tune_hz = float(center_hz) / f_factor
        # args=NULL in C API
        self._soapy.set_frequency(self._dev, self._SOAPY_SDR_RX, self._chan, tune_hz)
    def _read_block(self, nsamps: int):
        np = self._np
        buff = np.empty(nsamps, np.complex64)
        got = 0
        timeout_us = max(50000, int(self.dwell_ms * 1000))

        while got < nsamps and self._running:
            view = buff[got:]
            ptr = view.ctypes.data_as(ctypes.c_void_p)
            buffs = (ctypes.c_void_p * 1)(ptr)
            ret = self._soapy.read_stream(self._dev, self._rx_stream, buffs, nsamps - got, timeout_us=timeout_us)
            if ret > 0:
                got += ret
                continue
            if ret == 0:
                continue
            # Negative is an error (timeouts are typically negative too); keep going unless it persists.
            # We'll just break and zero-fill remaining.
            break

        if got < nsamps:
            buff[got:] = 0

        return buff
    def _psd_db(self, x):
        np = self._np
        n = int(self.fft_size)
        if len(x) < n:
            xx = np.zeros(n, np.complex64)
            xx[-len(x):] = x
            x = xx
        else:
            x = x[-n:]

        window = np.hanning(n).astype(np.float32)
        X = np.fft.fftshift(np.fft.fft(x * window))
        p = (np.abs(X) ** 2) / (np.sum(window ** 2) + 1e-12)
        return 10.0 * np.log10(p + 1e-12)

    def _fft_freq_axis(self, center_hz: float):
        np = self._np
        n = int(self.fft_size)
        freqs = np.fft.fftshift(np.fft.fftfreq(n, d=1.0 / float(self.sample_rate_hz)))
        return center_hz + freqs

    def _bin_psd_to_band(self, band: Dict[str, Any], freqs_hz, psd_db):
        np = self._np
        start_hz = float(band["start_hz"])
        stop_hz = float(band["stop_hz"])
        bw = float(self.bin_width_hz)

        m = (freqs_hz >= start_hz) & (freqs_hz < stop_hz)
        if not np.any(m):
            return None

        f_sel = freqs_hz[m]
        p_sel = psd_db[m]

        n_bins = int(max(1, math.ceil((stop_hz - start_hz) / bw)))
        powers = []
        for i in range(n_bins):
            lo = start_hz + i * bw
            hi = min(stop_hz, lo + bw)
            mm = (f_sel >= lo) & (f_sel < hi)
            if np.any(mm):
                powers.append(float(np.max(p_sel[mm])))
            else:
                powers.append(float(np.min(p_sel)))

        return {"low_hz": start_hz, "bin_width_hz": bw, "powers_dbm": powers}

    def _handle_frame(self, band: Dict[str, Any], frame: Dict[str, Any]) -> None:
        powers_raw = frame.get("powers_dbm", [])
        if not powers_raw:
            return

        cal_offset = self._net_cal_offset_db()
        powers = [float(p) + cal_offset for p in powers_raw]

        sorted_p = sorted(powers)
        if len(sorted_p) > 10:
            cutoff = int(len(sorted_p) * 0.8)
            noise_candidates = sorted_p[:cutoff]
        else:
            noise_candidates = sorted_p
        median_noise = statistics.median(noise_candidates)

        if self._noise_floor is None:
            self._noise_floor = median_noise
        else:
            alpha = 0.1
            self._noise_floor = (1 - alpha) * self._noise_floor + alpha * median_noise

        self.noise_floor_updated.emit(self._noise_floor)

        if self.use_local_noise_floor:
            abs_threshold = self._noise_floor + float(self.threshold_db)
        else:
            abs_threshold = float(self.threshold_db)

        low_hz = float(frame["low_hz"])
        bin_w = float(frame["bin_width_hz"])
        f_factor = self._freq_factor()

        detections: List[Dict[str, Any]] = []
        max_power = None
        max_freq_mhz = None

        now = time.time()
        hold = float(self.min_hold_time_s)

        band_start_hz = float(band.get("start_hz", float("-inf")))
        band_stop_hz = float(band.get("stop_hz", float("inf")))

        for idx in range(len(powers)):
            p_cal = powers[idx]
            p_raw = float(powers_raw[idx])

            center_hz_raw = low_hz + (idx + 0.5) * bin_w
            # Guard: some sweep backends may yield bins slightly outside the requested range.
            # Only consider bins that fall within the band limits.
            if center_hz_raw < band_start_hz or center_hz_raw > band_stop_hz:
                continue
            center_hz = center_hz_raw * f_factor

            freq_mhz_raw = center_hz_raw / 1e6
            freq_mhz = center_hz / 1e6

            key = round(freq_mhz, 6)
            st = self._hold_state.get(key)

            if p_cal >= abs_threshold:
                if st is None or not st.get("above", False):
                    st = {"first_seen": now, "last_seen": now, "above": True}
                    self._hold_state[key] = st
                else:
                    st["last_seen"] = now
                    st["above"] = True

                dwell = 0.0 if st["first_seen"] is None else now - st["first_seen"]

                if hold <= 0 or dwell >= hold:
                    detections.append(
                        {
                            "freq_mhz": freq_mhz,
                            "freq_mhz_raw": freq_mhz_raw,
                            "power_dbm": p_cal,
                            "power_dbm_raw": p_raw,
                            "cal_offset_db": cal_offset,
                            "freq_ppm": float(self.freq_ppm),
                            "timestamp": now,
                            "band": band.get("name", ""),
                            "source": self.source_id,
                        }
                    )
            else:
                if st is not None and st.get("above", False):
                    st["above"] = False
                    st["first_seen"] = None
                    st["last_seen"] = now

            if max_power is None or p_cal > max_power:
                max_power = p_cal
                max_freq_mhz = freq_mhz

        cleanup_limit = max(hold * 2.0, 10.0)
        stale_keys = []
        for k, st in self._hold_state.items():
            last_seen = st.get("last_seen")
            if last_seen is not None and (now - last_seen) > cleanup_limit:
                stale_keys.append(k)
        for k in stale_keys:
            del self._hold_state[k]

        span_txt = f"{band['start_mhz']:.3f}-{band['stop_mhz']:.3f} MHz"
        if max_power is not None and max_freq_mhz is not None:
            line = f"Max: {max_power:.1f} dB at {max_freq_mhz:.6f} MHz (span {span_txt})"
            if self.only_above_threshold:
                if max_power >= abs_threshold:
                    self.log_message.emit(line)
            else:
                self.log_message.emit(line)

        if detections:
            self.detections_found.emit(detections)

    @QtCore.pyqtSlot()
    def run(self):
        try:
            self._ensure_device()
            while self._running:
                cycle_start = time.time()
                any_band = False

                for band in self.bands:
                    if not self._running:
                        break
                    if not band.get("enabled"):
                        continue

                    any_band = True
                    start_hz = float(band["start_hz"])
                    stop_hz = float(band["stop_hz"])
                    center_hz = (start_hz + stop_hz) / 2.0

                    try:
                        self._set_center_freq(center_hz)
                        if self.settle_ms > 0:
                            time.sleep(float(self.settle_ms) / 1000.0)

                        psd_acc = None
                        n_avg = max(1, int(self.avg_frames))
                        for _i in range(n_avg):
                            if not self._running:
                                break
                            x = self._read_block(int(self.fft_size))
                            if len(x) < 8:
                                continue
                            psd = self._psd_db(x)
                            psd_acc = psd if psd_acc is None else (psd_acc + psd)

                        if psd_acc is None:
                            continue

                        psd_mean = psd_acc / float(n_avg)
                        freqs = self._fft_freq_axis(center_hz)
                        frame = self._bin_psd_to_band(band, freqs, psd_mean)
                        if frame is not None:
                            self._handle_frame(band, frame)

                        if self.dwell_ms > 0:
                            time.sleep(min(0.05, float(self.dwell_ms) / 1000.0))

                    except Exception as e:
                        self.log_message.emit(f"SoapySDR error: {e}")
                        time.sleep(1.0)

                if not self._running:
                    break

                if not any_band:
                    self.log_message.emit("No bands enabled; worker sleeping.")
                    time.sleep(1.0)

                if self.interval_ms > 0:
                    elapsed_ms = (time.time() - cycle_start) * 1000.0
                    remaining = self.interval_ms - elapsed_ms
                    if remaining > 0:
                        time.sleep(remaining / 1000.0)
        finally:
            try:
                self._close_device()
            except Exception:
                pass
            self.finished.emit()

# ---------------------------------------------------------------------------
# Main Window
# ---------------------------------------------------------------------------

class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("HackRF Watchdog")

        # ATAK bridge window
        self.atak_bridge = AtakBridge(self)
        self.atak_window = AtakBridgeWindow(self.atak_bridge, parent=self)
        self.atak_window.show()
        self.atak_bridge.status_changed.connect(lambda s: self.append_log(f"ATAK: {s}"))

        self.worker_thread: Optional[QtCore.QThread] = None
        self.worker: Optional[SweepWorker] = None

        # Parallel mode (multi HackRF) state
        self.parallel_threads: List[QtCore.QThread] = []
        self.parallel_workers: List[SweepWorker] = []
        self.parallel_serials: List[Optional[str]] = []
        self._parallel_active = False
        self._parallel_finished = 0
        self._noise_by_source: Dict[str, float] = {}

        self.detections: Dict[float, Dict[str, Any]] = {}
        self.current_noise_floor: Optional[float] = None
        self.sound_effects: Dict[str, QSoundEffect] = {}

        self.current_bin_width: int = 250_000

        self.bias_tee_requested: bool = False
        self.bias_tee_engaged: bool = False

        self._build_ui()
        self._create_timers()
        self.refresh_device_list()
        self._apply_band_assignment_enabled()
        self._load_settings()

    def _build_ui(self):
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)

        main_layout = QtWidgets.QVBoxLayout(central)

        # Top bar
        top_bar = QtWidgets.QHBoxLayout()
        self.start_btn = QtWidgets.QPushButton("Start")
        self.stop_btn = QtWidgets.QPushButton("Stop")
        self.stop_btn.setEnabled(False)
        self.status_label = QtWidgets.QLabel("Idle")

        top_bar.addWidget(self.start_btn)
        top_bar.addWidget(self.stop_btn)
        top_bar.addWidget(self.status_label)
        top_bar.addStretch(1)

        # Mode (single SDR vs multi SDR)
        top_bar.addWidget(QtWidgets.QLabel("Mode:"))
        self.mode_combo = QtWidgets.QComboBox()
        self.mode_combo.addItems([MODE_SINGLE, MODE_PARALLEL])
        self.mode_combo.setToolTip(
            "Single SDR: current behavior (time-slice bands).\n"
            "Parallel SDRs: future multi-device support (HackRF first)."
        )
        top_bar.addWidget(self.mode_combo)

        top_bar.addWidget(QtWidgets.QLabel("Perf:"))
        self.perf_combo = QtWidgets.QComboBox()
        self.perf_combo.addItems(list(PERF_PRESETS))
        self.perf_combo.setToolTip(
            "Controls UI/log throttling for responsiveness vs CPU.\\n"
            "Max: fastest tripwire. CPU-Lite: best for Raspberry Pi/DragonOS.\\n"
            "Custom: set by editing Advanced performance values."
        )
        self.perf_combo.setCurrentText(PERF_BALANCED)
        top_bar.addWidget(self.perf_combo)

        self.advanced_toggle = QtWidgets.QToolButton()
        self.advanced_toggle.setText("Advanced")
        self.advanced_toggle.setCheckable(True)
        self.advanced_toggle.setChecked(False)
        self.advanced_toggle.setArrowType(QtCore.Qt.RightArrow)
        self.advanced_toggle.setToolButtonStyle(QtCore.Qt.ToolButtonTextBesideIcon)
        top_bar.addWidget(self.advanced_toggle)

        self.atak_btn = QtWidgets.QPushButton("ATAK Bridge")
        top_bar.addWidget(self.atak_btn)

        self.clear_log_btn = QtWidgets.QPushButton("Clear log")
        top_bar.addWidget(self.clear_log_btn)

        self.dark_mode_checkbox = QtWidgets.QCheckBox("Dark mode")
        top_bar.addWidget(self.dark_mode_checkbox)

        main_layout.addLayout(top_bar)


        # Advanced performance (collapsed by default)
        self.advanced_group = QtWidgets.QGroupBox("Advanced performance")
        self.advanced_group.setVisible(False)
        adv_layout = QtWidgets.QGridLayout(self.advanced_group)

        r = 0
        adv_layout.addWidget(QtWidgets.QLabel("UI table refresh (ms):"), r, 0)
        self.ui_refresh_spin = QtWidgets.QSpinBox()
        self.ui_refresh_spin.setRange(50, 5000)
        self.ui_refresh_spin.setSingleStep(50)
        self.ui_refresh_spin.setValue(250)
        adv_layout.addWidget(self.ui_refresh_spin, r, 1)

        adv_layout.addWidget(QtWidgets.QLabel("Log flush (ms):"), r, 2)
        self.log_flush_spin = QtWidgets.QSpinBox()
        self.log_flush_spin.setRange(50, 2000)
        self.log_flush_spin.setSingleStep(50)
        self.log_flush_spin.setValue(100)
        adv_layout.addWidget(self.log_flush_spin, r, 3)

        r += 1
        adv_layout.addWidget(QtWidgets.QLabel("Max span log rate:"), r, 0)
        self.max_log_combo = QtWidgets.QComboBox()
        self.max_log_combo.addItem("Off", userData=0.0)
        self.max_log_combo.addItem("1 Hz", userData=1.0)
        self.max_log_combo.addItem("2 Hz", userData=0.5)
        self.max_log_combo.addItem("4 Hz", userData=0.25)
        self.max_log_combo.addItem("10 Hz", userData=0.1)
        self.max_log_combo.setCurrentIndex(3)  # 4 Hz default
        self.max_log_combo.setToolTip("Controls how often per-worker 'Max:' lines are logged. Detections are unaffected.")
        adv_layout.addWidget(self.max_log_combo, r, 1)

        adv_layout.addWidget(QtWidgets.QLabel("Log max lines:"), r, 2)
        self.log_max_lines_spin = QtWidgets.QSpinBox()
        self.log_max_lines_spin.setRange(500, 50000)
        self.log_max_lines_spin.setSingleStep(500)
        self.log_max_lines_spin.setValue(5000)
        self.log_max_lines_spin.setToolTip("Prevents the log widget from growing without bound (important on slow CPUs).")
        adv_layout.addWidget(self.log_max_lines_spin, r, 3)

        main_layout.addWidget(self.advanced_group)


        # ---------------- Detection settings group (LEFT) ----------------
        det_group = QtWidgets.QGroupBox("Detection settings")
        det_layout = QtWidgets.QGridLayout(det_group)

        row = 0
        det_layout.addWidget(QtWidgets.QLabel("Threshold (dB)"), row, 0)
        self.threshold_spin = QtWidgets.QDoubleSpinBox()
        self.threshold_spin.setDecimals(1)
        self.threshold_spin.setRange(0.0, 120.0)
        self.threshold_spin.setSingleStep(0.5)
        self.threshold_spin.setValue(3.0)
        det_layout.addWidget(self.threshold_spin, row, 1)

        self.only_above_threshold_cb = QtWidgets.QCheckBox("Only show detections above threshold")
        self.only_above_threshold_cb.setChecked(True)
        det_layout.addWidget(self.only_above_threshold_cb, row, 2, 1, 2)

        row += 1
        self.use_noise_floor_cb = QtWidgets.QCheckBox("Use local noise floor")
        self.use_noise_floor_cb.setChecked(True)
        det_layout.addWidget(self.use_noise_floor_cb, row, 0, 1, 2)

        self.noise_floor_label = QtWidgets.QLabel("Noise floor: --.- dB")
        det_layout.addWidget(self.noise_floor_label, row, 2, 1, 2)

        row += 1
        self.eff_threshold_label = QtWidgets.QLabel("Effective threshold: --.- dB")
        det_layout.addWidget(self.eff_threshold_label, row, 0, 1, 4)

        row += 1
        det_layout.addWidget(QtWidgets.QLabel("Persistence / hold time (s)"), row, 0)
        self.persistence_spin = QtWidgets.QDoubleSpinBox()
        self.persistence_spin.setDecimals(1)
        self.persistence_spin.setRange(0.0, 3600.0)
        self.persistence_spin.setSingleStep(0.1)
        self.persistence_spin.setValue(1.5)
        det_layout.addWidget(self.persistence_spin, row, 1)

        det_layout.addWidget(QtWidgets.QLabel("Interval (ms)"), row, 2)
        self.interval_spin = QtWidgets.QSpinBox()
        self.interval_spin.setRange(0, 60000)
        self.interval_spin.setSingleStep(50)
        self.interval_spin.setValue(0)
        det_layout.addWidget(self.interval_spin, row, 3)

        row += 1
        self.beep_checkbox = QtWidgets.QCheckBox("Beep on detection")
        self.beep_checkbox.setChecked(False)
        det_layout.addWidget(self.beep_checkbox, row, 0, 1, 4)

        row += 1
        det_layout.addWidget(QtWidgets.QLabel("Alarm sound"), row, 0)
        self.beep_sound_combo = QtWidgets.QComboBox()
        self.beep_sound_combo.addItem("System beep (default)", userData="system")
        self.beep_sound_combo.addItem("Soft ding", userData="soft_ding")
        self.beep_sound_combo.addItem("Short chirp", userData="short_chirp")
        self.beep_sound_combo.addItem("Alarm", userData="alarm")
        det_layout.addWidget(self.beep_sound_combo, row, 1, 1, 3)

        row += 1
        det_layout.addWidget(QtWidgets.QLabel("Antenna/LNA gain (dB)"), row, 0)
        self.cal_gain_spin = QtWidgets.QDoubleSpinBox()
        self.cal_gain_spin.setDecimals(1)
        self.cal_gain_spin.setRange(-200.0, 200.0)
        self.cal_gain_spin.setSingleStep(0.5)
        self.cal_gain_spin.setValue(0.0)
        det_layout.addWidget(self.cal_gain_spin, row, 1)

        det_layout.addWidget(QtWidgets.QLabel("Feedline loss (dB)"), row, 2)
        self.cal_loss_spin = QtWidgets.QDoubleSpinBox()
        self.cal_loss_spin.setDecimals(1)
        self.cal_loss_spin.setRange(0.0, 200.0)
        self.cal_loss_spin.setSingleStep(0.5)
        self.cal_loss_spin.setValue(0.0)
        det_layout.addWidget(self.cal_loss_spin, row, 3)

        row += 1
        self.cal_net_label = QtWidgets.QLabel("Net power offset: +0.0 dB (gain − loss)")
        det_layout.addWidget(self.cal_net_label, row, 0, 1, 4)

        row += 1
        det_layout.addWidget(QtWidgets.QLabel("Freq correction (ppm)"), row, 0)
        self.ppm_spin = QtWidgets.QDoubleSpinBox()
        self.ppm_spin.setDecimals(1)
        self.ppm_spin.setRange(-2000.0, 2000.0)
        self.ppm_spin.setSingleStep(0.5)
        self.ppm_spin.setValue(0.0)
        det_layout.addWidget(self.ppm_spin, row, 1)

        # ---------------- Device group (RIGHT) ----------------
        device_group = QtWidgets.QGroupBox("Device")
        dev_layout = QtWidgets.QGridLayout(device_group)

        dev_layout.addWidget(QtWidgets.QLabel("Type:"), 0, 0)
        self.device_type_combo = QtWidgets.QComboBox()
        self.device_type_combo.addItems([DEVICE_HACKRF, DEVICE_SOAPY])
        dev_layout.addWidget(self.device_type_combo, 0, 1, 1, 2)

        self.device_select_label = QtWidgets.QLabel("Device:")
        dev_layout.addWidget(self.device_select_label, 1, 0)
        self.device_combo = QtWidgets.QComboBox()
        dev_layout.addWidget(self.device_combo, 1, 1, 1, 2)


        # SoapySDR (experimental) settings (hidden unless selected)
        self.soapy_args_label = QtWidgets.QLabel("Soapy args:")
        self.soapy_args_edit = QtWidgets.QLineEdit("driver=bladerf")
        self.soapy_args_edit.setToolTip("Example: driver=bladerf  (later: add serial=..., etc.)")
        dev_layout.addWidget(self.soapy_args_label, 4, 0)
        dev_layout.addWidget(self.soapy_args_edit, 4, 1, 1, 2)

        # SoapySDR time-slice tuning (shown in Soapy mode and/or Parallel mode)
        self.soapy_rate_label = QtWidgets.QLabel("Sample rate (MHz):")
        self.soapy_rate_spin = QtWidgets.QDoubleSpinBox()
        self.soapy_rate_spin.setDecimals(1)
        self.soapy_rate_spin.setRange(1.0, 80.0)
        self.soapy_rate_spin.setSingleStep(1.0)
        self.soapy_rate_spin.setValue(20.0)

        self.soapy_bw_label = QtWidgets.QLabel("Bandwidth (MHz):")
        self.soapy_bw_spin = QtWidgets.QDoubleSpinBox()
        self.soapy_bw_spin.setDecimals(1)
        self.soapy_bw_spin.setRange(1.0, 80.0)
        self.soapy_bw_spin.setSingleStep(1.0)
        self.soapy_bw_spin.setValue(20.0)

        self.soapy_gain_label = QtWidgets.QLabel("Gain (dB):")
        self.soapy_gain_spin = QtWidgets.QDoubleSpinBox()
        self.soapy_gain_spin.setDecimals(0)
        self.soapy_gain_spin.setRange(0.0, 70.0)
        self.soapy_gain_spin.setSingleStep(1.0)
        self.soapy_gain_spin.setValue(30.0)

        self.soapy_dwell_label = QtWidgets.QLabel("Dwell (ms):")
        self.soapy_dwell_spin = QtWidgets.QSpinBox()
        self.soapy_dwell_spin.setRange(10, 5000)
        self.soapy_dwell_spin.setSingleStep(10)
        self.soapy_dwell_spin.setValue(250)

        self.soapy_settle_label = QtWidgets.QLabel("Settle (ms):")
        self.soapy_settle_spin = QtWidgets.QSpinBox()
        self.soapy_settle_spin.setRange(0, 1000)
        self.soapy_settle_spin.setSingleStep(5)
        self.soapy_settle_spin.setValue(40)

        self.soapy_fft_label = QtWidgets.QLabel("FFT size:")
        self.soapy_fft_combo = QtWidgets.QComboBox()
        for n in (1024, 2048, 4096, 8192):
            self.soapy_fft_combo.addItem(str(n), userData=int(n))
        self.soapy_fft_combo.setCurrentText("4096")

        self.soapy_avg_label = QtWidgets.QLabel("Avg frames:")
        self.soapy_avg_spin = QtWidgets.QSpinBox()
        self.soapy_avg_spin.setRange(1, 50)
        self.soapy_avg_spin.setSingleStep(1)
        self.soapy_avg_spin.setValue(4)

        dev_layout.addWidget(self.soapy_rate_label, 5, 0)
        dev_layout.addWidget(self.soapy_rate_spin, 5, 1)
        dev_layout.addWidget(self.soapy_bw_label, 5, 2)
        dev_layout.addWidget(self.soapy_bw_spin, 5, 3)

        dev_layout.addWidget(self.soapy_gain_label, 6, 0)
        dev_layout.addWidget(self.soapy_gain_spin, 6, 1)
        dev_layout.addWidget(self.soapy_dwell_label, 6, 2)
        dev_layout.addWidget(self.soapy_dwell_spin, 6, 3)

        dev_layout.addWidget(self.soapy_settle_label, 7, 0)
        dev_layout.addWidget(self.soapy_settle_spin, 7, 1)
        dev_layout.addWidget(self.soapy_fft_label, 7, 2)
        dev_layout.addWidget(self.soapy_fft_combo, 7, 3)

        dev_layout.addWidget(self.soapy_avg_label, 8, 0)
        dev_layout.addWidget(self.soapy_avg_spin, 8, 1)

        self.soapy_args_label.hide()
        self.soapy_args_edit.hide()
        self.soapy_rate_label.hide()
        self.soapy_rate_spin.hide()
        self.soapy_bw_label.hide()
        self.soapy_bw_spin.hide()
        self.soapy_gain_label.hide()
        self.soapy_gain_spin.hide()
        self.soapy_dwell_label.hide()
        self.soapy_dwell_spin.hide()
        self.soapy_settle_label.hide()
        self.soapy_settle_spin.hide()
        self.soapy_fft_label.hide()
        self.soapy_fft_combo.hide()
        self.soapy_avg_label.hide()
        self.soapy_avg_spin.hide()

        self.refresh_devices_btn = QtWidgets.QPushButton("Refresh")
        dev_layout.addWidget(self.refresh_devices_btn, 2, 2)

        self.bias_tee_checkbox = QtWidgets.QCheckBox("Bias-T / antenna power")
        dev_layout.addWidget(self.bias_tee_checkbox, 3, 0, 1, 3)

        # Make the Device box a bit narrower so it doesn't steal width
        device_group.setMaximumWidth(420)

        # ---------------- Top row layout: Detection (left) + Device (right) ----------------
        top_row = QtWidgets.QHBoxLayout()
        det_group.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Preferred)
        device_group.setSizePolicy(QtWidgets.QSizePolicy.Maximum, QtWidgets.QSizePolicy.Preferred)

        top_row.addWidget(det_group, 1)
        top_row.addWidget(device_group, 0)

        main_layout.addLayout(top_row)

        # ---------------- Band configuration group ----------------
        band_group = QtWidgets.QGroupBox("Band configurations")
        bg_layout = QtWidgets.QGridLayout(band_group)

        row = 0
        bg_layout.addWidget(QtWidgets.QLabel("Band"), row, 0)
        bg_layout.addWidget(QtWidgets.QLabel("Enabled"), row, 1)
        bg_layout.addWidget(QtWidgets.QLabel("Start (MHz)"), row, 2)
        bg_layout.addWidget(QtWidgets.QLabel("Stop (MHz)"), row, 3)
        bg_layout.addWidget(QtWidgets.QLabel("Device"), row, 4)

        row += 1
        self.bandA_label = QtWidgets.QLabel("Band A")
        self.bandA_enable = QtWidgets.QCheckBox()
        self.bandA_enable.setChecked(True)
        self.bandA_start = QtWidgets.QDoubleSpinBox()
        self.bandA_start.setDecimals(3)
        self.bandA_start.setRange(1.0, 6000.0)
        self.bandA_start.setValue(900.0)
        self.bandA_stop = QtWidgets.QDoubleSpinBox()
        self.bandA_stop.setDecimals(3)
        self.bandA_stop.setRange(1.0, 6000.0)
        self.bandA_stop.setValue(930.0)
        bg_layout.addWidget(self.bandA_label, row, 0)
        bg_layout.addWidget(self.bandA_enable, row, 1)
        bg_layout.addWidget(self.bandA_start, row, 2)
        bg_layout.addWidget(self.bandA_stop, row, 3)

        self.bandA_device = QtWidgets.QComboBox()
        bg_layout.addWidget(self.bandA_device, row, 4)

        row += 1
        self.bandB_label = QtWidgets.QLabel("Band B")
        self.bandB_enable = QtWidgets.QCheckBox()
        self.bandB_enable.setChecked(True)
        self.bandB_start = QtWidgets.QDoubleSpinBox()
        self.bandB_start.setDecimals(3)
        self.bandB_start.setRange(1.0, 6000.0)
        self.bandB_start.setValue(144.0)
        self.bandB_stop = QtWidgets.QDoubleSpinBox()
        self.bandB_stop.setDecimals(3)
        self.bandB_stop.setRange(1.0, 6000.0)
        self.bandB_stop.setValue(148.0)
        bg_layout.addWidget(self.bandB_label, row, 0)
        bg_layout.addWidget(self.bandB_enable, row, 1)
        bg_layout.addWidget(self.bandB_start, row, 2)
        bg_layout.addWidget(self.bandB_stop, row, 3)

        self.bandB_device = QtWidgets.QComboBox()
        bg_layout.addWidget(self.bandB_device, row, 4)

        row += 1
        self.bandC_label = QtWidgets.QLabel("Band C")
        self.bandC_enable = QtWidgets.QCheckBox()
        self.bandC_enable.setChecked(True)
        self.bandC_start = QtWidgets.QDoubleSpinBox()
        self.bandC_start.setDecimals(3)
        self.bandC_start.setRange(1.0, 6000.0)
        self.bandC_start.setValue(420.0)
        self.bandC_stop = QtWidgets.QDoubleSpinBox()
        self.bandC_stop.setDecimals(3)
        self.bandC_stop.setRange(1.0, 6000.0)
        self.bandC_stop.setValue(450.0)
        bg_layout.addWidget(self.bandC_label, row, 0)
        bg_layout.addWidget(self.bandC_enable, row, 1)
        bg_layout.addWidget(self.bandC_start, row, 2)
        bg_layout.addWidget(self.bandC_stop, row, 3)

        self.bandC_device = QtWidgets.QComboBox()
        bg_layout.addWidget(self.bandC_device, row, 4)

        row += 1
        bg_layout.addWidget(QtWidgets.QLabel("Bin width (Hz)"), row, 0)
        self.bin_width_spin = QtWidgets.QSpinBox()
        self.bin_width_spin.setRange(2445, 5_000_000)
        self.bin_width_spin.setSingleStep(1000)
        self.bin_width_spin.setValue(250_000)
        bg_layout.addWidget(self.bin_width_spin, row, 1)

        self.auto_bin_checkbox = QtWidgets.QCheckBox("Auto")
        self.auto_bin_checkbox.setChecked(True)
        bg_layout.addWidget(self.auto_bin_checkbox, row, 2)

        self.max_bins_spin = QtWidgets.QSpinBox()
        self.max_bins_spin.setRange(50, 2000)
        self.max_bins_spin.setSingleStep(50)
        self.max_bins_spin.setValue(400)
        bg_layout.addWidget(self.max_bins_spin, row, 3)

        main_layout.addWidget(band_group)

        # ---------------- Bottom splitter ----------------
        bottom_splitter = QtWidgets.QSplitter(QtCore.Qt.Vertical)

        self.table = QtWidgets.QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Frequency (MHz)", "Power (dB)", "Age (s)"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)

        self.log_edit = QtWidgets.QTextEdit()
        self.log_edit.setReadOnly(True)
        font = QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont)
        font.setPointSize(14)
        self.log_edit.setFont(font)
        # Keep log widget from growing without bound (performance on slower CPUs)
        self.log_edit.document().setMaximumBlockCount(5000)

        bottom_splitter.addWidget(self.table)
        bottom_splitter.addWidget(self.log_edit)
        bottom_splitter.setStretchFactor(0, 3)
        bottom_splitter.setStretchFactor(1, 1)

        main_layout.addWidget(bottom_splitter, 1)

        # ---------------- Connections ----------------
        self.start_btn.clicked.connect(self.start_watchdog)
        self.stop_btn.clicked.connect(self.stop_watchdog)
        self.dark_mode_checkbox.toggled.connect(self.apply_dark_mode)
        self.clear_log_btn.clicked.connect(self.clear_log)
        self.refresh_devices_btn.clicked.connect(self.refresh_device_list)
        self.device_type_combo.currentIndexChanged.connect(self._on_device_type_changed)
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        self.perf_combo.currentIndexChanged.connect(self._on_perf_preset_changed)
        self.advanced_toggle.toggled.connect(self._toggle_advanced)
        self.ui_refresh_spin.valueChanged.connect(self._on_perf_param_changed)
        self.log_flush_spin.valueChanged.connect(self._on_perf_param_changed)
        self.max_log_combo.currentIndexChanged.connect(self._on_perf_param_changed)
        self.log_max_lines_spin.valueChanged.connect(self._on_perf_param_changed)
        self.use_noise_floor_cb.toggled.connect(self.on_use_noise_floor_toggled)
        self.threshold_spin.valueChanged.connect(self.on_threshold_changed)
        self.auto_bin_checkbox.toggled.connect(self.on_auto_bin_toggled)

        self.cal_gain_spin.valueChanged.connect(self.on_cal_changed)
        self.cal_loss_spin.valueChanged.connect(self.on_cal_changed)
        self.ppm_spin.valueChanged.connect(self.on_ppm_changed)

        self.atak_btn.clicked.connect(self.show_atak_bridge)
        self.soapy_args_edit.textChanged.connect(self.refresh_band_device_dropdowns)

        self.on_auto_bin_toggled(self.auto_bin_checkbox.isChecked())
        self.on_use_noise_floor_toggled(self.use_noise_floor_cb.isChecked())
        self.on_cal_changed()
        self.update_effective_threshold_label()

    def show_atak_bridge(self):
        self.atak_window.show()
        self.atak_window.raise_()
        self.atak_window.activateWindow()

    def _create_timers(self):
        # Detection table refresh (UI). The detection engine can run much faster.
        self.update_timer = QtCore.QTimer(self)
        self.update_timer.setInterval(int(getattr(self, 'ui_refresh_spin', None).value() if hasattr(self, 'ui_refresh_spin') else 250))  # UI refresh
        self.update_timer.timeout.connect(self.refresh_detection_table)
        self.update_timer.start()

        # Log buffering: workers may emit very frequently; we flush in batches to keep UI responsive.
        self._pending_log_lines: List[str] = []
        self._dropped_log_lines = 0

        self.log_flush_timer = QtCore.QTimer(self)
        self.log_flush_timer.setInterval(int(getattr(self, 'log_flush_spin', None).value() if hasattr(self, 'log_flush_spin') else 100))  # Log flush
        self.log_flush_timer.timeout.connect(self._flush_log_buffer)
        self.log_flush_timer.start()

    

    def _toggle_advanced(self, checked: bool):
        try:
            self.advanced_group.setVisible(bool(checked))
            self.advanced_toggle.setArrowType(QtCore.Qt.DownArrow if checked else QtCore.Qt.RightArrow)
        except Exception:
            pass

    def _any_soapy_selected(self) -> bool:
        try:
            if str(self.device_type_combo.currentText()) == DEVICE_SOAPY:
                return True
            for cb in (getattr(self, "bandA_device", None), getattr(self, "bandB_device", None), getattr(self, "bandC_device", None)):
                if cb is None:
                    continue
                a = cb.currentData()
                if isinstance(a, dict) and str(a.get("kind")) == "soapy":
                    return True
        except Exception:
            pass
        return False

    def _on_perf_preset_changed(self):
        preset = str(self.perf_combo.currentText())
        if preset not in PERF_PRESETS:
            return
        self._apply_performance_preset(preset)

    def _on_perf_param_changed(self):
        # Any manual tweak moves us into Custom (unless we're applying a preset programmatically).
        if getattr(self, "_applying_perf", False):
            return
        try:
            self.perf_combo.setCurrentText(PERF_CUSTOM)
        except Exception:
            pass
        self._apply_performance_runtime()

    def _apply_performance_preset(self, preset: str):
        if preset not in PERF_PRESETS:
            return
        self._applying_perf = True
        try:
            if preset == PERF_MAX_TRIPWIRE:
                self.ui_refresh_spin.setValue(250)   # 4 Hz
                self.log_flush_spin.setValue(100)    # 10 Hz
                self.max_log_combo.setCurrentText("4 Hz")
                self.log_max_lines_spin.setValue(5000)
                if self._any_soapy_selected():
                    self.soapy_fft_combo.setCurrentText("4096")
                    self.soapy_avg_spin.setValue(2)
            elif preset == PERF_BALANCED:
                self.ui_refresh_spin.setValue(500)   # 2 Hz
                self.log_flush_spin.setValue(200)    # 5 Hz
                self.max_log_combo.setCurrentText("2 Hz")
                self.log_max_lines_spin.setValue(5000)
                if self._any_soapy_selected():
                    self.soapy_fft_combo.setCurrentText("4096")
                    self.soapy_avg_spin.setValue(4)
            elif preset == PERF_CPU_LITE:
                self.ui_refresh_spin.setValue(1000)  # 1 Hz
                self.log_flush_spin.setValue(500)    # 2 Hz
                self.max_log_combo.setCurrentText("1 Hz")
                self.log_max_lines_spin.setValue(3000)
                if self._any_soapy_selected():
                    self.soapy_fft_combo.setCurrentText("2048")
                    self.soapy_avg_spin.setValue(4)
            elif preset == PERF_CUSTOM:
                # Keep current values
                pass
        finally:
            self._applying_perf = False

        self._apply_performance_runtime()

    def _current_max_log_period_s(self) -> float:
        try:
            return float(self.max_log_combo.currentData() or 0.25)
        except Exception:
            return 0.25

    def _apply_performance_runtime(self):
        # Update timers and apply to active workers (no need to restart).
        try:
            if hasattr(self, "update_timer") and self.update_timer is not None:
                self.update_timer.setInterval(int(self.ui_refresh_spin.value()))
        except Exception:
            pass
        try:
            if hasattr(self, "log_flush_timer") and self.log_flush_timer is not None:
                self.log_flush_timer.setInterval(int(self.log_flush_spin.value()))
        except Exception:
            pass

        # Apply max span log throttling to workers
        try:
            period = self._current_max_log_period_s()
            if getattr(self, "worker", None) is not None and hasattr(self.worker, "max_log_period_s"):
                self.worker.max_log_period_s = float(period)
            for w in getattr(self, "parallel_workers", []) or []:
                if hasattr(w, "max_log_period_s"):
                    w.max_log_period_s = float(period)
        except Exception:
            pass

    def refresh_device_list(self):
        """Refresh the *single-mode* device selector and the per-band device dropdowns."""
        dtype = str(self.device_type_combo.currentText())

        self.device_combo.blockSignals(True)
        self.device_combo.clear()

        if dtype == DEVICE_SOAPY:
            self.device_select_label.setText("Soapy device:")
            # Allow using the free-form args field.
            custom_args = str(self.soapy_args_edit.text() or "driver=bladerf")
            self.device_combo.addItem(f"Custom (use args) – {custom_args}", userData=custom_args)

            soapy = list_soapy_devices()
            for dev in soapy:
                self.device_combo.addItem(f"{dev['label']}", userData=str(dev["args_str"]))
        else:
            self.device_select_label.setText("HackRF:")
            self.device_combo.addItem("Default (first HackRF)", userData=None)
            devices = list_hackrf_devices()
            for dev in devices:
                label = f"HackRF {dev['index']} – {dev['serial']}"
                self.device_combo.addItem(label, userData=dev["serial"])

        self.device_combo.blockSignals(False)
        self.refresh_band_device_dropdowns()
    # --- WATCHDOG MULTI-SDR STEP1 helpers ---


    def _band_device_widgets(self) -> List[QtWidgets.QComboBox]:
        return [self.bandA_device, self.bandB_device, self.bandC_device]

    def _encode_assignment(self, data: Any) -> str:
        # Store small stable tokens in QSettings
        if not isinstance(data, dict):
            return "auto"
        kind = str(data.get("kind", "auto"))
        if kind == "hackrf":
            return f"hackrf:{data.get('serial','')}"
        if kind == "soapy":
            # args may contain commas; store verbatim after prefix
            return f"soapy:{data.get('args','')}"
        return "auto"

    def _decode_assignment(self, token: str) -> Dict[str, Any]:
        t = (token or "").strip()
        if t.startswith("hackrf:"):
            return {"kind": "hackrf", "serial": t.split(":", 1)[1]}
        if t.startswith("soapy:"):
            return {"kind": "soapy", "args": t.split(":", 1)[1]}
        return {"kind": "auto"}

    
    def _device_options(self) -> List[Dict[str, Any]]:
        opts: List[Dict[str, Any]] = []
        opts.append({"label": "Auto", "data": {"kind": "auto"}})

        # HackRFs
        devices = list_hackrf_devices()
        for dev in devices:
            serial = dev.get("serial")
            if not serial:
                continue
            label = f"HackRF {dev.get('index','?')} – {serial}"
            opts.append({"label": label, "data": {"kind": "hackrf", "serial": str(serial)}})

        # SoapySDR devices (enumerated if available)
        soapy_list = list_soapy_devices()
        for dev in soapy_list:
            args_str = str(dev.get("args_str") or "")
            if not args_str:
                continue
            label = f"SoapySDR – {dev.get('label')}"
            opts.append({"label": label, "data": {"kind": "soapy", "args": args_str}})

        # Always include a "custom args" option so users can type args manually.
        custom = str(self.soapy_args_edit.text() or "driver=bladerf")
        opts.append({"label": f"SoapySDR – Custom ({custom})", "data": {"kind": "soapy", "args": custom}})

        return opts

    def refresh_band_device_dropdowns(self) -> None:
        # Save current selections
        current = [self._encode_assignment(cb.currentData()) for cb in self._band_device_widgets()]

        opts = self._device_options()
        for i, cb in enumerate(self._band_device_widgets()):
            cb.blockSignals(True)
            cb.clear()
            for opt in opts:
                cb.addItem(opt["label"], userData=opt["data"])

            token = current[i] if i < len(current) else "auto"
            target = self._decode_assignment(token)
            chosen = 0
            for j in range(cb.count()):
                d = cb.itemData(j)
                if not (isinstance(d, dict) and d.get("kind") == target.get("kind")):
                    continue

                if d.get("kind") == "hackrf" and str(d.get("serial")) == str(target.get("serial")):
                    chosen = j
                    break

                if d.get("kind") == "soapy":
                    if str(d.get("args") or "") == str(target.get("args") or ""):
                        chosen = j
                        break
            cb.setCurrentIndex(chosen)
            cb.blockSignals(False)

        self._apply_band_assignment_enabled()

    def _apply_band_assignment_enabled(self) -> None:
        # Enable per-band assignment dropdowns only in Parallel mode
        mode = str(self.mode_combo.currentText())
        enabled = (mode == MODE_PARALLEL)
        for cb in self._band_device_widgets():
            cb.setEnabled(enabled)

    def _on_device_type_changed(self, _idx: int = 0) -> None:
        """Show/hide device-specific UI controls.

        - In Single mode: Device Type selects the backend (HackRF vs SoapySDR).
        - In Parallel mode: per-band Device dropdowns drive assignment; we keep both
          HackRF and Soapy controls visible.
        """
        dtype = str(self.device_type_combo.currentText())
        mode = str(self.mode_combo.currentText())
        is_parallel = (mode == MODE_PARALLEL)
        is_soapy = (dtype == DEVICE_SOAPY)

        # In parallel mode we always show Soapy controls (you might assign a band to Soapy),
        # and we keep HackRF controls available too.
        show_soapy_controls = is_parallel or is_soapy

        self.soapy_args_label.setVisible(show_soapy_controls)
        self.soapy_args_edit.setVisible(show_soapy_controls)
        self.soapy_rate_label.setVisible(show_soapy_controls)
        self.soapy_rate_spin.setVisible(show_soapy_controls)
        self.soapy_bw_label.setVisible(show_soapy_controls)
        self.soapy_bw_spin.setVisible(show_soapy_controls)
        self.soapy_gain_label.setVisible(show_soapy_controls)
        self.soapy_gain_spin.setVisible(show_soapy_controls)
        self.soapy_dwell_label.setVisible(show_soapy_controls)
        self.soapy_dwell_spin.setVisible(show_soapy_controls)
        self.soapy_settle_label.setVisible(show_soapy_controls)
        self.soapy_settle_spin.setVisible(show_soapy_controls)
        self.soapy_fft_label.setVisible(show_soapy_controls)
        self.soapy_fft_combo.setVisible(show_soapy_controls)
        self.soapy_avg_label.setVisible(show_soapy_controls)
        self.soapy_avg_spin.setVisible(show_soapy_controls)

        # Device selector changes meaning depending on backend.
        # - HackRF: select a specific HackRF (or Default)
        # - Soapy: select an enumerated Soapy device (or Custom args)
        self.refresh_devices_btn.setEnabled(True)
        self.device_combo.setEnabled(True)
        # Bias-T only applies to HackRF devices; we leave the checkbox enabled but it will be ignored for Soapy.

        if not is_parallel and is_soapy:
            self.append_log("SoapySDR selected: running in time-slice mode (experimental).")

        # Keep lists fresh when switching backend
        self.refresh_device_list()

    def _on_mode_changed(self, _idx: int = 0) -> None:
        mode = str(self.mode_combo.currentText())
        self.append_log(f"Mode set to: {mode}")

        if mode == MODE_PARALLEL:
            # In Parallel mode you still may want to switch between HackRF vs SoapySDR
            # (to expose Soapy settings and refresh discovered devices for assignment).
            self.device_type_combo.setEnabled(True)
            self.device_type_combo.setToolTip(
                "In Parallel mode, per-band Device dropdowns choose the hardware. "
                "This selector controls which backend settings are shown and which devices are discovered."
            )
        else:
            self.device_type_combo.setEnabled(True)
            self.device_type_combo.setToolTip("Select HackRF vs SoapySDR backend for Single mode.")

        self._apply_band_assignment_enabled()

        # Update visibility of device controls (Soapy settings shown in Parallel)
        self._on_device_type_changed(0)
    def _start_parallel_hackrf(
        self,
        *,
        bands: List[Dict[str, Any]],
        bin_width_hz: int,
        threshold_db: float,
        use_local_noise_floor: bool,
        only_above_threshold: bool,
        min_hold_time_s: float,
        interval_ms: int,
        antenna_power: bool,
        cal_gain_db: float,
        cal_loss_db: float,
        freq_ppm: float,
    ) -> None:
        """Start one SweepWorker per connected HackRF and split enabled bands across them."""

        devices = list_hackrf_devices()
        serials = [d.get("serial") for d in devices if d.get("serial")]

        # Fallback: if hackrf_info parsing failed, honor the user's selected device (or default)
        if not serials:
            selected = self.device_combo.currentData()
            if selected:
                serials = [str(selected)]
            else:
                serials = [None]

        # Split bands round-robin across available devices
        assignments: Dict[Optional[str], List[Dict[str, Any]]] = {}
        for i, band in enumerate(bands):
            serial = serials[i % len(serials)]
            key = str(serial) if serial is not None else None
            assignments.setdefault(key, []).append(band)

        used = [(serial, blist) for serial, blist in assignments.items() if blist]
        if not used:
            QtWidgets.QMessageBox.warning(self, "Parallel mode", "No enabled bands to assign.")
            self.status_label.setText("Idle")
            self.start_btn.setEnabled(True)
            self.stop_btn.setEnabled(False)
            return

        # Reset any prior parallel state
        self._parallel_finished = 0
        self._noise_by_source = {}
        self.parallel_threads = []
        self.parallel_workers = []
        self.parallel_serials = [serial for (serial, _bl) in used]
        self._parallel_active = True

        # Bias-tee handling: enable per device (if requested)
        self.bias_tee_requested = bool(antenna_power)
        self.bias_tee_engaged = False
        if antenna_power:
            for serial in self.parallel_serials:
                try:
                    ok = set_bias_tee(True, self.append_log, serial=serial)
                    self.bias_tee_engaged = self.bias_tee_engaged or bool(ok)
                except Exception:
                    pass

        # Start workers
        for idx, (serial, blist) in enumerate(used):
            label = str(serial) if serial else f"HackRF-{idx}"

            thread = QtCore.QThread(self)
            worker = SweepWorker(
                bands=blist,
                bin_width_hz=int(bin_width_hz),
                threshold_db=float(threshold_db),
                use_local_noise_floor=bool(use_local_noise_floor),
                only_above_threshold=bool(only_above_threshold),
                min_hold_time_s=float(min_hold_time_s),
                interval_ms=int(interval_ms),
                start_delay_ms=int(idx * 350),
                device_arg=serial,
                antenna_power=bool(antenna_power),
                cal_gain_db=float(cal_gain_db),
                cal_loss_db=float(cal_loss_db),
                freq_ppm=float(freq_ppm),
                source_id=label,
            )
            worker.moveToThread(thread)

            thread.started.connect(worker.run)

            # Prefix logs with device label
            worker.log_message.connect(lambda msg, l=label: self.append_log(f"[{l}] {msg}"))

            # Average noise floor for display
            worker.noise_floor_updated.connect(lambda v, l=label: self._on_parallel_noise_floor(v, l))

            # Use existing detection pipeline (detections include 'source' now)
            worker.detections_found.connect(self.on_detections_found)

            worker.finished.connect(self._on_parallel_worker_finished)
            worker.finished.connect(thread.quit)
            worker.finished.connect(worker.deleteLater)
            thread.finished.connect(thread.deleteLater)

            self.parallel_workers.append(worker)
            self.parallel_threads.append(thread)
            thread.start()

        self.append_log(f"Parallel mode: started {len(self.parallel_workers)} worker(s) across {len(used)} device(s).")



    def _start_single_soapy(
        self,
        bands: List[Dict[str, Any]],
        bin_width_hz: int,
        threshold_db: float,
        use_local_noise_floor: bool,
        only_above_threshold: bool,
        min_hold_time_s: float,
        interval_ms: int,
        cal_gain_db: float,
        cal_loss_db: float,
        freq_ppm: float,
    ) -> None:
        # Basic availability check (gives a friendly error early)
        try:
            import numpy  # noqa: F401
        except Exception as e:
            QtWidgets.QMessageBox.critical(
                self,
                "NumPy not available",
                "SoapySDR time-slice mode requires NumPy.\n\n" f"Import error: {e}",
            )
            return

        if not soapy_capi_available():
            QtWidgets.QMessageBox.critical(
                self,
                "SoapySDR runtime not available",
                "SoapySDR time-slice mode requires the SoapySDR runtime (SoapySDR.dll).\n\n"
                "On Windows, install PothosSDR and ensure 'C:\\Program Files\\PothosSDR\\bin' is on PATH.",
            )
            return

# Soapy worker runs without HackRF bias-tee
        self.bias_tee_requested = False
        self.bias_tee_engaged = False

        # Prefer the selected Soapy device (if one is chosen); fall back to custom args field.
        selected = self.device_combo.currentData()
        if isinstance(selected, str) and selected.strip():
            soapy_args = str(selected).strip()
        else:
            soapy_args = str(self.soapy_args_edit.text() or "driver=bladerf")

        self.worker_thread = QtCore.QThread(self)
        self.worker = SoapyTimeSliceWorker(
            bands=bands,
            bin_width_hz=bin_width_hz,
            threshold_db=threshold_db,
            use_local_noise_floor=use_local_noise_floor,
            only_above_threshold=only_above_threshold,
            min_hold_time_s=min_hold_time_s,
            interval_ms=interval_ms,
            soapy_args=soapy_args,
            sample_rate_hz=float(self.soapy_rate_spin.value()) * 1e6,
            bandwidth_hz=float(self.soapy_bw_spin.value()) * 1e6,
            gain_db=float(self.soapy_gain_spin.value()),
            dwell_ms=int(self.soapy_dwell_spin.value()),
            settle_ms=int(self.soapy_settle_spin.value()),
            fft_size=int(self.soapy_fft_combo.currentData() or 4096),
            avg_frames=int(self.soapy_avg_spin.value()),
            cal_gain_db=cal_gain_db,
            cal_loss_db=cal_loss_db,
            freq_ppm=freq_ppm,
            source_id="Soapy",
        )

        self.worker.moveToThread(self.worker_thread)
        self.worker_thread.started.connect(self.worker.run)
        self.worker.log_message.connect(self.append_log)
        self.worker.noise_floor_updated.connect(self.on_noise_floor_updated)
        self.worker.detections_found.connect(self.on_detections_found)
        self.worker.finished.connect(self.on_worker_finished)

        self.worker_thread.start()
        self.append_log("Single SoapySDR mode started (time-slice).")

    def _start_parallel_manual(
        self,
        bands: List[Dict[str, Any]],
        bin_width_hz: int,
        threshold_db: float,
        use_local_noise_floor: bool,
        only_above_threshold: bool,
        min_hold_time_s: float,
        interval_ms: int,
        antenna_power: bool,
        cal_gain_db: float,
        cal_loss_db: float,
        freq_ppm: float,
    ) -> None:
        devices = list_hackrf_devices()
        hackrf_serials = [d.get("serial") for d in devices if d.get("serial")]
        hackrf_serials = [str(s) for s in hackrf_serials]

        # Group bands by assignment
        by_serial: Dict[str, List[Dict[str, Any]]] = {}
        soapy_by_args: Dict[str, List[Dict[str, Any]]] = {}
        auto_bands: List[Dict[str, Any]] = []

        for b in bands:
            a = b.get("assign")
            kind = str(a.get("kind")) if isinstance(a, dict) else "auto"
            if kind == "hackrf":
                serial = str(a.get("serial") or "")
                if not serial:
                    auto_bands.append(b)
                else:
                    by_serial.setdefault(serial, []).append(b)
            elif kind == "soapy":
                args = str(a.get("args") or "").strip()
                if not args:
                    args = str(self.soapy_args_edit.text() or "driver=bladerf")
                soapy_by_args.setdefault(args, []).append(b)
            else:
                auto_bands.append(b)

        if auto_bands and not hackrf_serials:
            QtWidgets.QMessageBox.critical(
                self,
                "No HackRF devices",
                "One or more bands are set to Auto (or HackRF) in Parallel mode, but no HackRF devices were found.",
            )
            return

        # Assign auto bands round-robin across available HackRFs
        if auto_bands:
            rr = 0
            for b in auto_bands:
                serial = hackrf_serials[rr % len(hackrf_serials)]
                by_serial.setdefault(serial, []).append(b)
                rr += 1

        
        # Prevent accidental use of Soapy "hackrf" factory for HackRF devices.
        # HackRFs should be handled by hackrf_sweep workers for reliability.
        if soapy_by_args:
            redirected: Dict[str, List[Dict[str, Any]]] = {}
            for args, blist in list(soapy_by_args.items()):
                drv = ""
                serial = ""
                for part in args.split(","):
                    if "=" in part:
                        k, v = part.split("=", 1)
                        k = k.strip().lower()
                        v = v.strip()
                        if k == "driver":
                            drv = v.lower()
                        elif k == "serial":
                            serial = v
                if drv == "hackrf":
                    if serial:
                        by_serial.setdefault(serial, []).extend(blist)
                    else:
                        auto_bands.extend(blist)
                else:
                    redirected[args] = blist
            soapy_by_args = redirected
# Validate explicit HackRF assignments exist
        for serial in list(by_serial.keys()):
            if serial not in hackrf_serials:
                QtWidgets.QMessageBox.warning(
                    self,
                    "HackRF not found",
                    f"Band assignment requested HackRF serial {serial}, but it was not detected.\n"
                    "That band will be skipped.",
                )
                del by_serial[serial]

        if soapy_by_args:
            # Soapy time-slice uses the SoapySDR runtime via ctypes (no python SoapySDR module needed).
            try:
                import numpy  # noqa: F401
            except Exception as e:
                QtWidgets.QMessageBox.critical(
                    self,
                    "NumPy not available",
                    "One or more bands are assigned to SoapySDR, but NumPy is not available.\n\n" f"Import error: {e}",
                )
                return

            if not soapy_capi_available():
                QtWidgets.QMessageBox.critical(
                    self,
                    "SoapySDR runtime not available",
                    "One or more bands are assigned to SoapySDR, but the SoapySDR runtime (SoapySDR.dll) could not be loaded.\n\n"
                    "On Windows, install PothosSDR and ensure 'C:\\Program Files\\PothosSDR\\bin' is on PATH.",
                )
                return

        if not by_serial and not soapy_by_args:
            QtWidgets.QMessageBox.warning(
                self,
                "No workers",
                "No valid device assignments were available to run.",
            )
            return

        # Parallel worker setup
        self._parallel_active = True
        self.parallel_threads = []
        self.parallel_workers = []
        self.parallel_serials = []

        # Bias-tee only applies to HackRF devices
        self.bias_tee_requested = bool(self.bias_tee_checkbox.isChecked())
        self.bias_tee_engaged = False

        if self.bias_tee_requested:
            ok_any = False
            for serial in by_serial.keys():
                if set_bias_tee(True, self.append_log, serial=serial):
                    ok_any = True
            self.bias_tee_engaged = ok_any

        # HackRF workers
        for serial, b_list in by_serial.items():
            if not b_list:
                continue
            self.parallel_serials.append(serial)

            thread = QtCore.QThread(self)
            worker = SweepWorker(
                bands=b_list,
                bin_width_hz=bin_width_hz,
                threshold_db=threshold_db,
                use_local_noise_floor=use_local_noise_floor,
                only_above_threshold=only_above_threshold,
                min_hold_time_s=min_hold_time_s,
                interval_ms=interval_ms,
                device_arg=serial,
                antenna_power=antenna_power,
                cal_gain_db=cal_gain_db,
                cal_loss_db=cal_loss_db,
                freq_ppm=freq_ppm,
                source_id=str(serial),
                start_delay_ms=max(0, (len(self.parallel_serials) - 1) * 350),
            )

            worker.moveToThread
            try:
                worker.max_log_period_s = float(self._current_max_log_period_s())
            except Exception:
                pass

            worker.moveToThread(thread)
            thread.started.connect(worker.run)
            worker.log_message.connect(self.append_log)
            worker.noise_floor_updated.connect(lambda v, s=serial: self._on_parallel_noise_floor(v, s))
            worker.detections_found.connect(self.on_detections_found)
            worker.finished.connect(self._on_parallel_worker_finished)
            worker.finished.connect(thread.quit)
            worker.finished.connect(worker.deleteLater)
            thread.finished.connect(thread.deleteLater)

            self.parallel_threads.append(thread)
            self.parallel_workers.append(worker)

        # Soapy workers (one per unique args string)
        if soapy_by_args:
            for soapy_args, b_list in soapy_by_args.items():
                if not b_list:
                    continue

                thread = QtCore.QThread(self)
                worker = SoapyTimeSliceWorker(
                    bands=b_list,
                    bin_width_hz=bin_width_hz,
                    threshold_db=threshold_db,
                    use_local_noise_floor=use_local_noise_floor,
                    only_above_threshold=only_above_threshold,
                    min_hold_time_s=min_hold_time_s,
                    interval_ms=interval_ms,
                    soapy_args=str(soapy_args),
                    sample_rate_hz=float(self.soapy_rate_spin.value()) * 1e6,
                    bandwidth_hz=float(self.soapy_bw_spin.value()) * 1e6,
                    gain_db=float(self.soapy_gain_spin.value()),
                    dwell_ms=int(self.soapy_dwell_spin.value()),
                    settle_ms=int(self.soapy_settle_spin.value()),
                    fft_size=int(self.soapy_fft_combo.currentData() or 4096),
                    avg_frames=int(self.soapy_avg_spin.value()),
                    cal_gain_db=cal_gain_db,
                    cal_loss_db=cal_loss_db,
                    freq_ppm=freq_ppm,
                    source_id=f"Soapy:{soapy_args}",
                )

                worker.moveToThread(thread)
                thread.started.connect(worker.run)
                worker.log_message.connect(self.append_log)
                worker.noise_floor_updated.connect(lambda v, s=f"Soapy:{soapy_args}": self._on_parallel_noise_floor(v, s))
                worker.detections_found.connect(self.on_detections_found)
                worker.finished.connect(self._on_parallel_worker_finished)
                worker.finished.connect(thread.quit)
                worker.finished.connect(worker.deleteLater)
                thread.finished.connect(thread.deleteLater)

                self.parallel_threads.append(thread)
                self.parallel_workers.append(worker)


        # Start everything
        for thread in self.parallel_threads:
            thread.start()

        self.append_log(f"Parallel manual mode: started {len(self.parallel_workers)} worker(s).")

    def _on_parallel_noise_floor(self, value: float, label: str) -> None:
        self._noise_by_source[str(label)] = float(value)
        vals = list(self._noise_by_source.values())
        if vals:
            self.on_noise_floor_updated(sum(vals) / float(len(vals)))

    def _on_parallel_worker_finished(self) -> None:
        self._parallel_finished += 1
        if self._parallel_finished >= max(1, len(self.parallel_workers)):
            self.append_log("All parallel workers finished.")
            self._cleanup_parallel()

    def _cleanup_parallel(self) -> None:
        # Ensure bias-tee is off on all devices we touched
        if self.bias_tee_requested or self.bias_tee_engaged:
            for serial in self.parallel_serials:
                try:
                    set_bias_tee(False, self.append_log, serial=serial)
                except Exception:
                    pass
        # Stop and dispose QThreads cleanly (prevents 'QThread: Destroyed while thread is still running')
        for thread in list(getattr(self, "parallel_threads", [])):
            try:
                if thread is not None and thread.isRunning():
                    thread.quit()
                    thread.wait(3000)
            except Exception:
                pass

        self.bias_tee_requested = False
        self.bias_tee_engaged = False

        self._parallel_active = False
        self.parallel_serials = []
        self.parallel_workers = []
        self.parallel_threads = []
        self._noise_by_source = {}

        self.status_label.setText("Idle")
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)

    def _load_settings(self) -> None:
        s = QtCore.QSettings()
        try:
            mode = s.value("watchdog/mode", MODE_SINGLE)
            if mode in (MODE_SINGLE, MODE_PARALLEL):
                self.mode_combo.setCurrentText(str(mode))
        except Exception:
            pass

        try:
            dtype = s.value("watchdog/device_type", DEVICE_HACKRF)
            if dtype in (DEVICE_HACKRF, DEVICE_SOAPY):
                self.device_type_combo.setCurrentText(str(dtype))
        except Exception:
            pass

        # Performance preset
        preset = PERF_BALANCED
        try:
            preset = str(s.value("watchdog/perf_preset", PERF_BALANCED))
            if preset not in PERF_PRESETS:
                preset = PERF_BALANCED
        except Exception:
            preset = PERF_BALANCED

        self._applying_perf = True
        try:
            self.perf_combo.setCurrentText(preset)
            if preset == PERF_CUSTOM:
                self.ui_refresh_spin.setValue(int(s.value("watchdog/ui_refresh_ms", self.ui_refresh_spin.value())))
                self.log_flush_spin.setValue(int(s.value("watchdog/log_flush_ms", self.log_flush_spin.value())))
                self.log_max_lines_spin.setValue(int(s.value("watchdog/log_max_lines", self.log_max_lines_spin.value())))
                p = float(s.value("watchdog/max_log_period_s", float(self._current_max_log_period_s())))
                # choose closest option
                best_i = 0
                best_d = None
                for i in range(self.max_log_combo.count()):
                    d = float(self.max_log_combo.itemData(i) or 0.0)
                    if best_d is None or abs(d - p) < abs(best_d - p):
                        best_d = d
                        best_i = i
                self.max_log_combo.setCurrentIndex(best_i)
            else:
                self._apply_performance_preset(preset)
        except Exception:
            pass
        finally:
            self._applying_perf = False
        self._apply_performance_runtime()

        # Soapy params
        try:
            self.soapy_args_edit.setText(str(s.value("watchdog/soapy_args", self.soapy_args_edit.text())))
            self.soapy_rate_spin.setValue(float(s.value("watchdog/soapy_rate_mhz", self.soapy_rate_spin.value())))
            self.soapy_bw_spin.setValue(float(s.value("watchdog/soapy_bw_mhz", self.soapy_bw_spin.value())))
            self.soapy_gain_spin.setValue(float(s.value("watchdog/soapy_gain_db", self.soapy_gain_spin.value())))
            self.soapy_dwell_spin.setValue(int(s.value("watchdog/soapy_dwell_ms", self.soapy_dwell_spin.value())))
            self.soapy_settle_spin.setValue(int(s.value("watchdog/soapy_settle_ms", self.soapy_settle_spin.value())))
            fft = int(s.value("watchdog/soapy_fft_size", int(self.soapy_fft_combo.currentData() or 4096)))
            self.soapy_fft_combo.setCurrentText(str(fft))
            self.soapy_avg_spin.setValue(int(s.value("watchdog/soapy_avg_frames", self.soapy_avg_spin.value())))
        except Exception:
            pass

        # Band assignment tokens (Parallel mode)
        try:
            a = str(s.value("watchdog/band_assign_A", "auto"))
            b = str(s.value("watchdog/band_assign_B", "auto"))
            c = str(s.value("watchdog/band_assign_C", "auto"))
            self.refresh_band_device_dropdowns()

            for cb, tok in [(self.bandA_device, a), (self.bandB_device, b), (self.bandC_device, c)]:
                target = self._decode_assignment(tok)
                chosen = 0
                for j in range(cb.count()):
                    d = cb.itemData(j)
                    if isinstance(d, dict) and d.get("kind") == target.get("kind"):
                        if d.get("kind") == "hackrf" and str(d.get("serial")) == str(target.get("serial")):
                            chosen = j
                            break
                        if d.get("kind") == "soapy":
                            chosen = j
                            break
                cb.setCurrentIndex(chosen)
        except Exception:
            pass

        self._on_mode_changed(0)
        self._on_device_type_changed(0)
    def _save_settings(self) -> None:
        try:
            s = QtCore.QSettings()
            s.setValue("watchdog/mode", str(self.mode_combo.currentText()))
            s.setValue("watchdog/device_type", str(self.device_type_combo.currentText()))
            s.setValue("watchdog/perf_preset", str(self.perf_combo.currentText()))
            s.setValue("watchdog/ui_refresh_ms", int(self.ui_refresh_spin.value()))
            s.setValue("watchdog/log_flush_ms", int(self.log_flush_spin.value()))
            s.setValue("watchdog/log_max_lines", int(self.log_max_lines_spin.value()))
            s.setValue("watchdog/max_log_period_s", float(self._current_max_log_period_s()))

            # Step 3: per-band assignments + soapy params
            s.setValue("watchdog/band_assign_A", self._encode_assignment(self.bandA_device.currentData()))
            s.setValue("watchdog/band_assign_B", self._encode_assignment(self.bandB_device.currentData()))
            s.setValue("watchdog/band_assign_C", self._encode_assignment(self.bandC_device.currentData()))

            s.setValue("watchdog/soapy_args", str(self.soapy_args_edit.text()))
            s.setValue("watchdog/soapy_rate_mhz", float(self.soapy_rate_spin.value()))
            s.setValue("watchdog/soapy_bw_mhz", float(self.soapy_bw_spin.value()))
            s.setValue("watchdog/soapy_gain_db", float(self.soapy_gain_spin.value()))
            s.setValue("watchdog/soapy_dwell_ms", int(self.soapy_dwell_spin.value()))
            s.setValue("watchdog/soapy_settle_ms", int(self.soapy_settle_spin.value()))
            s.setValue("watchdog/soapy_fft_size", int(self.soapy_fft_combo.currentData() or 4096))
            s.setValue("watchdog/soapy_avg_frames", int(self.soapy_avg_spin.value()))
        except Exception:
            pass
    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        try:
            self.stop_watchdog()
        except Exception:
            pass
        try:
            self._save_settings()
        except Exception:
            pass
        super().closeEvent(event)

    def net_cal_offset_db(self) -> float:
        return float(self.cal_gain_spin.value()) - float(self.cal_loss_spin.value())

    def on_cal_changed(self):
        net = self.net_cal_offset_db()
        self.cal_net_label.setText(f"Net power offset: {net:+.1f} dB (gain − loss)")
        if self.worker is not None:
            self.worker.cal_gain_db = float(self.cal_gain_spin.value())
            self.worker.cal_loss_db = float(self.cal_loss_spin.value())
        self.update_effective_threshold_label()

    def on_ppm_changed(self, value: float):
        if self.worker is not None:
            self.worker.freq_ppm = float(value)

    def on_use_noise_floor_toggled(self, checked: bool):
        if self.worker is not None:
            self.worker.use_local_noise_floor = checked
        self.update_effective_threshold_label()

    def on_threshold_changed(self, value: float):
        if self.worker is not None:
            self.worker.threshold_db = float(value)
        self.update_effective_threshold_label()

    def update_effective_threshold_label(self):
        thr = float(self.threshold_spin.value())
        net = self.net_cal_offset_db()

        if self.use_noise_floor_cb.isChecked():
            if self.current_noise_floor is None:
                self.eff_threshold_label.setText(
                    f"Effective threshold: (waiting for noise floor, offset {thr:.1f} dB; cal {net:+.1f} applied)"
                )
            else:
                abs_thr = float(self.current_noise_floor) + thr
                self.eff_threshold_label.setText(
                    f"Effective threshold: {abs_thr:.1f} dB (noise {self.current_noise_floor:.1f} + {thr:.1f}; cal {net:+.1f} applied)"
                )
        else:
            self.eff_threshold_label.setText(
                f"Effective threshold: {thr:.1f} dB (absolute; cal {net:+.1f} applied)"
            )

    def on_auto_bin_toggled(self, checked: bool):
        self.bin_width_spin.setEnabled(not checked)
        self.max_bins_spin.setEnabled(checked)

    def choose_auto_bin_width(self, bands: List[Dict[str, Any]]) -> int:
        max_bins = self.max_bins_spin.value() or 400
        max_span_hz = 0.0
        for b in bands:
            if not b.get("enabled", True):
                continue
            span_hz = (b["stop_mhz"] - b["start_mhz"]) * 1e6
            max_span_hz = max(max_span_hz, span_hz)

        if max_span_hz <= 0:
            return int(self.bin_width_spin.value()) or 250_000

        raw_bin = max_span_hz / float(max_bins)
        raw_bin = max(10_000, min(1_000_000, raw_bin))
        nice = int(round(raw_bin / 10_000.0)) * 10_000
        return nice if nice > 0 else 10_000

    def play_alarm_sound(self):
        if not self.beep_checkbox.isChecked():
            return

        mode = self.beep_sound_combo.currentData()
        if mode == "system" or mode is None:
            QtWidgets.QApplication.beep()
            return

        filename_map = {
            "soft_ding": "soft_ding.wav",
            "short_chirp": "short_chirp.wav",
            "alarm": "alarm.wav",
        }
        fname = filename_map.get(mode)
        if not fname:
            QtWidgets.QApplication.beep()
            return

        base_dir = os.path.dirname(os.path.abspath(__file__))
        sound_path = os.path.join(base_dir, "sounds", fname)
        if not os.path.exists(sound_path):
            QtWidgets.QApplication.beep()
            return

        effect = self.sound_effects.get(mode)
        if effect is None:
            effect = QSoundEffect(self)
            effect.setSource(QtCore.QUrl.fromLocalFile(sound_path))
            effect.setVolume(0.9)
            self.sound_effects[mode] = effect
        effect.play()

    def start_watchdog(self):
        if self.worker is not None or self._parallel_active:
            return
        # STEP1: Mode/device selection is UI-only except for baseline HackRF single-SDR path.
        mode = str(self.mode_combo.currentText())
        dtype = str(self.device_type_combo.currentText())
        bands = []
        for name, enabled_cb, start_spin, stop_spin in [
            ("A", self.bandA_enable, self.bandA_start, self.bandA_stop),
            ("B", self.bandB_enable, self.bandB_start, self.bandB_stop),
            ("C", self.bandC_enable, self.bandC_start, self.bandC_stop),
        ]:
            if not enabled_cb.isChecked():
                continue
            start_mhz = start_spin.value()
            stop_mhz = stop_spin.value()
            if stop_mhz <= start_mhz:
                continue
            bands.append(
                {
                    "name": name,
                    "enabled": True,
                    "start_mhz": start_mhz,
                    "stop_mhz": stop_mhz,
                    "start_hz": start_mhz * 1e6,
                    "stop_hz": stop_mhz * 1e6,
                }
            )

        if not bands:
            QtWidgets.QMessageBox.warning(self, "No bands", "Enable at least one band.")
            return

        if self.auto_bin_checkbox.isChecked():
            bin_width = int(self.choose_auto_bin_width(bands))
            self.append_log(f"Auto bin width selected: {bin_width} Hz")
        else:
            bin_width = int(self.bin_width_spin.value())

        self.current_bin_width = bin_width

        threshold_db = float(self.threshold_spin.value())
        use_noise_floor = self.use_noise_floor_cb.isChecked()
        only_above = self.only_above_threshold_cb.isChecked()
        interval_ms = int(self.interval_spin.value())
        # NOTE: Interval clamping for HackRF one-shot mode is handled inside SweepWorker.
        # In continuous mode, interval_ms=0 enables maximum processing rate.
        min_hold = float(self.persistence_spin.value())
        device_arg = self.device_combo.currentData()

        antenna_power = self.bias_tee_checkbox.isChecked()
        cal_gain = float(self.cal_gain_spin.value())
        cal_loss = float(self.cal_loss_spin.value())
        ppm = float(self.ppm_spin.value())

        self.detections.clear()
        self.status_label.setText("Sweeping...")
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)

        # Parallel mode (Step 3): manual per-band device assignment.
        if mode == MODE_PARALLEL:
            # Attach per-band assignment metadata
            assign_map = {'A': self.bandA_device, 'B': self.bandB_device, 'C': self.bandC_device}
            for b in bands:
                cb = assign_map.get(str(b.get('name')))
                if cb is not None:
                    b['assign'] = cb.currentData()

            any_assigned = False
            for b in bands:
                a = b.get('assign')
                if isinstance(a, dict) and str(a.get('kind', 'auto')) != 'auto':
                    any_assigned = True
                    break

            if not any_assigned:
                # All bands set to Auto: keep the original HackRF round-robin behavior
                self._start_parallel_hackrf(
                    bands=bands,
                    bin_width_hz=bin_width,
                    threshold_db=threshold_db,
                    use_local_noise_floor=use_noise_floor,
                    only_above_threshold=only_above,
                    min_hold_time_s=min_hold,
                    interval_ms=interval_ms,
                    antenna_power=antenna_power,
                    cal_gain_db=cal_gain,
                    cal_loss_db=cal_loss,
                    freq_ppm=ppm,
                )
            else:
                self._start_parallel_manual(
                    bands=bands,
                    bin_width_hz=bin_width,
                    threshold_db=threshold_db,
                    use_local_noise_floor=use_noise_floor,
                    only_above_threshold=only_above,
                    min_hold_time_s=min_hold,
                    interval_ms=interval_ms,
                    antenna_power=antenna_power,
                    cal_gain_db=cal_gain,
                    cal_loss_db=cal_loss,
                    freq_ppm=ppm,
                )
            return

        # Single mode SoapySDR backend (time-slice)
        if mode == MODE_SINGLE and dtype == DEVICE_SOAPY:
            self._start_single_soapy(
                bands=bands,
                bin_width_hz=bin_width,
                threshold_db=threshold_db,
                use_local_noise_floor=use_noise_floor,
                only_above_threshold=only_above,
                min_hold_time_s=min_hold,
                interval_ms=interval_ms,
                cal_gain_db=cal_gain,
                cal_loss_db=cal_loss,
                freq_ppm=ppm,
            )
            return

        self.bias_tee_requested = bool(antenna_power)
        self.bias_tee_engaged = False
        if antenna_power:
            self.bias_tee_engaged = set_bias_tee(True, self.append_log, serial=device_arg)

        self.worker_thread = QtCore.QThread(self)
        self.worker = SweepWorker(
            bands=bands,
            bin_width_hz=bin_width,
            threshold_db=threshold_db,
            use_local_noise_floor=use_noise_floor,
            only_above_threshold=only_above,
            min_hold_time_s=min_hold,
            interval_ms=interval_ms,
            device_arg=device_arg,
            antenna_power=antenna_power,
            cal_gain_db=cal_gain,
            cal_loss_db=cal_loss,
            freq_ppm=ppm,
        )
        try:
            self.worker.max_log_period_s = float(self._current_max_log_period_s())
        except Exception:
            pass
        self.worker.moveToThread(self.worker_thread)

        self.worker_thread.started.connect(self.worker.run)
        self.worker.finished.connect(self.on_worker_finished)
        self.worker.finished.connect(self.worker_thread.quit)
        self.worker.finished.connect(self.worker.deleteLater)
        self.worker_thread.finished.connect(self.worker_thread.deleteLater)

        self.worker.log_message.connect(self.append_log)
        self.worker.noise_floor_updated.connect(self.on_noise_floor_updated)
        self.worker.detections_found.connect(self.on_detections_found)

        self.worker_thread.start()
        self.append_log("Starting watchdog...")

    def stop_watchdog(self):
        if self._parallel_active:
            self.append_log("Stopping watchdog (parallel)...")
            self.status_label.setText("Stopping...")

            for w in list(self.parallel_workers):
                try:
                    w.stop()
                except Exception:
                    pass

            # Turn off bias-tee on all used devices immediately
            if self.bias_tee_requested or self.bias_tee_engaged:
                for serial in self.parallel_serials:
                    try:
                        set_bias_tee(False, self.append_log, serial=serial)
                    except Exception:
                        pass
                self.bias_tee_requested = False
                self.bias_tee_engaged = False

            return

        if self.worker is not None:
            self.append_log("Stopping watchdog...")
            self.status_label.setText("Stopping...")
            self.worker.stop()

        if self.bias_tee_requested:
            device_arg = self.device_combo.currentData()
            set_bias_tee(False, self.append_log, serial=device_arg)
            self.bias_tee_engaged = False
            self.bias_tee_requested = False

        if self.worker is None:
            self.status_label.setText("Idle")

    def on_worker_finished(self):
        self.append_log("Worker finished.")

        if self.bias_tee_requested or self.bias_tee_engaged:
            device_arg = self.device_combo.currentData()
            set_bias_tee(False, self.append_log, serial=device_arg)
            self.bias_tee_requested = False
            self.bias_tee_engaged = False

        self.status_label.setText("Idle")
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.worker = None
        self.worker_thread = None

    @QtCore.pyqtSlot(float)
    def on_noise_floor_updated(self, value: float):
        self.current_noise_floor = value
        self.noise_floor_label.setText(f"Noise floor: {value:.1f} dB")
        self.update_effective_threshold_label()

    @QtCore.pyqtSlot(list)
    def on_detections_found(self, detections: List[Dict[str, Any]]):
        for d in detections:
            freq = round(float(d["freq_mhz"]), 6)
            existing = self.detections.get(freq)
            if existing is None or float(d["power_dbm"]) > float(existing["power_dbm"]):
                self.detections[freq] = d
            else:
                existing["timestamp"] = d["timestamp"]

        if detections:
            self.play_alarm_sound()

        for d in detections:
            try:
                self.atak_bridge.send_detection(d, noise_floor=self.current_noise_floor)
            except Exception as e:
                self.append_log(f"ATAK send error: {e}")

    def refresh_detection_table(self):
        now = time.time()
        items = sorted(self.detections.items(), key=lambda kv: kv[1]["timestamp"], reverse=True)
        self.table.setRowCount(len(items))

        for row, (freq, d) in enumerate(items):
            age = now - float(d["timestamp"])
            self.table.setItem(row, 0, QtWidgets.QTableWidgetItem(f"{freq:.6f}"))
            self.table.setItem(row, 1, QtWidgets.QTableWidgetItem(f"{float(d['power_dbm']):.1f}"))
            self.table.setItem(row, 2, QtWidgets.QTableWidgetItem(f"{age:.1f}"))

    def append_log(self, text: str):
        # Buffer log lines; flushed by timer to keep UI smooth even with fast sweep processing.
        if not hasattr(self, "_pending_log_lines"):
            self._pending_log_lines = []
            self._dropped_log_lines = 0

        self._pending_log_lines.append(text)

        # Prevent unbounded growth if UI is busy; drop oldest lines and report once.
        MAX_PENDING = 5000
        if len(self._pending_log_lines) > MAX_PENDING:
            drop = len(self._pending_log_lines) - MAX_PENDING
            del self._pending_log_lines[:drop]
            self._dropped_log_lines += drop


    def _trim_log_widget(self):
        """Trim the QTextEdit log to the configured maximum number of lines (document blocks)."""
        try:
            max_lines = int(self.log_max_lines_spin.value()) if hasattr(self, "log_max_lines_spin") else 5000
        except Exception:
            max_lines = 5000

        # Safety bounds
        if max_lines < 100:
            max_lines = 100

        doc = self.log_edit.document()
        try:
            block_count = doc.blockCount()
        except Exception:
            return

        extra = block_count - max_lines
        if extra <= 0:
            return

        # Remove the oldest `extra` blocks in one operation.
        cursor = QtGui.QTextCursor(doc)
        cursor.movePosition(QtGui.QTextCursor.Start)
        cursor.movePosition(QtGui.QTextCursor.NextBlock, QtGui.QTextCursor.KeepAnchor, extra)
        cursor.removeSelectedText()
        # Remove leading newline left behind (if any)
        if doc.characterCount() > 1:
            cursor.deleteChar()

    def _flush_log_buffer(self):
        if not hasattr(self, "_pending_log_lines"):
            return
        if not self._pending_log_lines:
            return

        # Flush in manageable batches
        BATCH = 200
        lines = self._pending_log_lines[:BATCH]
        del self._pending_log_lines[:BATCH]

        if self._dropped_log_lines:
            lines.insert(0, f"[log] Dropped {self._dropped_log_lines} lines to keep UI responsive.")
            self._dropped_log_lines = 0

        # Append without per-line cursor churn
        self.log_edit.setUpdatesEnabled(False)
        try:
            self.log_edit.append("\n".join(lines))
            self.log_edit.moveCursor(QtGui.QTextCursor.End)
            self._trim_log_widget()
        finally:
            self.log_edit.setUpdatesEnabled(True)

    def clear_log(self):
        if hasattr(self, "_pending_log_lines"):
            self._pending_log_lines.clear()
        if hasattr(self, "_dropped_log_lines"):
            self._dropped_log_lines = 0
        self.log_edit.clear()

    def apply_dark_mode(self, enabled: bool):
        if enabled:
            self.setStyleSheet(
                """
                QWidget { background-color: #222; color: #eee; }
                QGroupBox { border: 1px solid #444; margin-top: 6px; }
                QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 3px 0 3px; }
                QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QTextEdit, QTableWidget {
                    background-color: #333; color: #eee; border: 1px solid #555;
                }
                QHeaderView::section { background-color: #333; color: #eee; }
                QPushButton { background-color: #444; color: #eee; border: 1px solid #666; padding: 3px 8px; }
                QPushButton:disabled { background-color: #333; color: #777; }
                """
            )
        else:
            self.setStyleSheet("")


def main():
    try:
        app = QtWidgets.QApplication(sys.argv)
        app.setOrganizationName("HackRF-Watchdog")
        app.setApplicationName("HackRF-Watchdog")

        win = MainWindow()
        win.resize(1200, 800)
        win.show()
        sys.exit(app.exec_())
    except Exception:
        # Always print and log (sys.excepthook also logs)
        traceback.print_exc()
        try:
            # If Qt is available, show a minimal error dialog
            app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
            QtWidgets.QMessageBox.critical(
                None,
                "Watchdog crashed at launch",
                "Watchdog crashed at launch.\n\n"
                "A crash log was written next to main.py as:\n"
                "  watchdog_crash.log\n\n"
                "Please paste the last ~50 lines of that log here."
            )
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()

import sys
import time
import statistics
import math
import subprocess
import os
import shutil
import ctypes
import ctypes.util
import inspect
from typing import List, Dict, Any, Optional
from collections import deque

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
from hackrf_watchdog.device_discovery import (
    list_hackrf_devices as _discover_hackrf_devices,
    list_soapy_devices as _discover_soapy_devices,
    parse_soapy_args as _parse_soapy_args,
)
from hackrf_watchdog.doctor import check_hackrf_backend, check_soapy_backend

try:
    from hackrf_watchdog.cf_tuner import CenterFrequencyTunerWindow
except Exception:
    CenterFrequencyTunerWindow = None

# ---------------------------------------------------------------------------
# WATCHDOG MULTI-SDR STEP1 (UI-only, no behavior change by default)
# ---------------------------------------------------------------------------
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
    return _discover_hackrf_devices()



# ---------------------------------------------------------------------------
# SoapySDR device detection (optional)
# ---------------------------------------------------------------------------

def list_soapy_devices() -> List[Dict[str, Any]]:
    return _discover_soapy_devices()

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

        # Generic device settings (e.g., biastee/bias_tx per driver)
        self._writeSetting = lib.SoapySDRDevice_writeSetting
        self._writeSetting.restype = None
        self._writeSetting.argtypes = [c_void_p, c_char_p, c_char_p]

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
        ret = self._setSampleRate(dev, int(direction), ctypes.c_size_t(chan), float(rate_hz))
        if int(ret) != 0:
            raise RuntimeError(f"SoapySDR setSampleRate failed (Fs={float(rate_hz)/1e6:.3f} MHz). {self.last_error()}")

    def set_bandwidth(self, dev, direction: int, chan: int, bw_hz: float):
        ret = self._setBandwidth(dev, int(direction), ctypes.c_size_t(chan), float(bw_hz))
        if int(ret) != 0:
            raise RuntimeError(f"SoapySDR setBandwidth failed (BW={float(bw_hz)/1e6:.3f} MHz). {self.last_error()}")

    def set_frequency(self, dev, direction: int, chan: int, freq_hz: float):
        # args = NULL
        self._setFrequency(dev, int(direction), ctypes.c_size_t(chan), float(freq_hz), ctypes.c_void_p(0))

    def set_gain(self, dev, direction: int, chan: int, gain_db: float):
        self._setGain(dev, int(direction), ctypes.c_size_t(chan), float(gain_db))

    def write_setting(self, dev, key: str, value: str):
        self._writeSetting(
            dev,
            str(key).encode("utf-8", errors="ignore"),
            str(value).encode("utf-8", errors="ignore"),
        )

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
        self._noise_mad = None
        self._noise_mad = None
        self._hold_state: Dict[float, Dict[str, Any]] = {}
        self._hit_times: Dict[float, deque] = {}  # for persistence (hits within a window)
        #dow)
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
                # HackRF scanning is intended to be continuous. In Single SDR mode, sweeping multiple
                # disjoint bands continuously would require sweeping the full union span (often huge),
                # so we limit each HackRF worker to ONE enabled band.
                bands_to_scan = enabled_bands
                if len(enabled_bands) > 1:
                    self.log_message.emit(
                        "Note: Continuous HackRF sweep supports one enabled band per HackRF worker. "
                        "Using the first enabled band only."
                    )
                    bands_to_scan = [enabled_bands[0]]
                use_continuous_hackrf = True if bands_to_scan else False


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

                for band in bands_to_scan:
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
        # Robust spread estimate: MAD (median absolute deviation)
        try:
            mad = statistics.median([abs(p - median_noise) for p in noise_candidates]) + 1e-9
        except Exception:
            mad = 1.0

        if self._noise_floor is None:
            self._noise_floor = median_noise
            self._noise_mad = mad
        else:
            alpha = 0.1
            self._noise_floor = (1 - alpha) * self._noise_floor + alpha * median_noise
            self._noise_mad = (1 - alpha) * float(getattr(self, "_noise_mad", mad)) + alpha * mad

        self.noise_floor_updated.emit(self._noise_floor)

        thr_db = float(band.get('threshold_db', self.threshold_db))

        use_nf = bool(band.get('use_noise_floor', self.use_local_noise_floor))
        only_above = bool(band.get('only_show_above', self.only_above_threshold))
        hold = float(band.get('hold_time_s', self.min_hold_time_s))

        if use_nf:
            # Robust threshold: median + k*MAD (k comes from Threshold control)
            mad = float(getattr(self, "_noise_mad", 1.0))
            abs_threshold = float(self._noise_floor) + (thr_db * mad)
        else:
            abs_threshold = thr_db

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
                    # Persistence filter: require at least 2 hits within a short window.
                    ht = self._hit_times.get(key)
                    if ht is None:
                        ht = deque()
                        self._hit_times[key] = ht
                    ht.append(now)
                    # keep only last 0.6s
                    while ht and (now - ht[0]) > 0.6:
                        ht.popleft()
                    if len(ht) < 2:
                        continue
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
                if only_above:
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
        antenna_power: bool = False,
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
        self._soapy_kv = _parse_soapy_args(self.soapy_args) or {"driver": "bladerf"}
        self._soapy_driver = str(self._soapy_kv.get("driver") or "unknown")
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
        self.antenna_power = bool(antenna_power)
        self.source_id = str(source_id)

        self._running = True
        self._noise_floor = None
        self._hold_state: Dict[float, Dict[str, Any]] = {}
        self._hit_times: Dict[float, deque] = {}  # for persistence (hits within a window)

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
        out = _parse_soapy_args(s)
        if not out:
            out = {"driver": "bladerf"}
        return out

    def _bias_setting_key(self) -> str:
        drv = str(self._soapy_driver or "").strip().lower()
        if "rtl" in drv:
            return "biastee"
        if "bladerf" in drv or "blade" in drv:
            return "biastee_rx"
        if "hackrf" in drv:
            return "bias_tx"
        return ""

    def _apply_bias_power(self, enabled: bool, *, quiet: bool = False) -> None:
        key = self._bias_setting_key()
        if not key:
            return
        val = "true" if bool(enabled) else "false"
        try:
            self._soapy.write_setting(self._dev, key, val)
            if not quiet:
                self.log_message.emit(
                    f"SoapySDR Bias-T {'ON' if enabled else 'OFF'} "
                    f"(driver={self._soapy_driver}, setting={key})"
                )
        except Exception as e:
            if not quiet:
                self.log_message.emit(
                    f"SoapySDR Bias-T control failed "
                    f"(driver={self._soapy_driver}, setting={key}): {e}"
                )

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
                "SoapySDR runtime not available (ctypes C-API load failed).\n"
                "Linux: install libsoapysdr0.8 + soapysdr-tools (+ soapysdr module for your SDR, e.g. soapysdr-module-bladerf).\n"
                "Windows: install PothosSDR and ensure SoapySDR.dll is on PATH (PothosSDR\\bin).\n"
                f"Requested Soapy target: driver={self._soapy_driver}, args='{self.soapy_args}'"
            )

        self._soapy = get_soapy_capi()
        self._SOAPY_SDR_RX = self._soapy.SOAPY_SDR_RX

        # Open device from argument string like: 'driver=bladerf,serial=...'
        self._dev = self._soapy.make(self.soapy_args)

        # Apply per-device Bias-T before streaming starts.
        self._apply_bias_power(self.antenna_power, quiet=False)

        # --- Normalize/guard sample-rate & bandwidth before touching the device ---
        args_l = self.soapy_args.lower()
        sr = max(1e6, float(self.sample_rate_hz))
        bw = max(1e6, float(self.bandwidth_hz))
        if bw > sr:
            bw = sr

        # Known-safe caps (prevents common "invalid sample rate" errors).
        # If the runtime/driver supports more, the user can still request it later once we add capability probing.
        if "driver=bladerf" in args_l or "bladerf" in args_l:
            sr = min(sr, 61.44e6)
            bw = min(bw, sr)

        self.sample_rate_hz = sr
        self.bandwidth_hz = bw

        # Configure (retry with saner rates if driver rejects the requested one)
        tried_sr = []
        try:
            self._soapy.set_sample_rate(self._dev, self._SOAPY_SDR_RX, self._chan, self.sample_rate_hz)
        except Exception as e:
            # Try a small list of common SDR rates, descending from requested -> safe.
            common = [61.44e6, 56e6, 40e6, 30e6, 20e6, 10e6, 5e6, 2e6, 1e6]
            # Ensure we start at or below the requested sr.
            common = [r for r in common if r <= float(self.sample_rate_hz) + 1] + [1e6]
            common = list(dict.fromkeys(common))  # unique, keep order
            ok = False
            for r in common:
                tried_sr.append(r)
                try:
                    self._soapy.set_sample_rate(self._dev, self._SOAPY_SDR_RX, self._chan, r)
                    self.sample_rate_hz = float(r)
                    # BW must never exceed SR
                    if self.bandwidth_hz > self.sample_rate_hz:
                        self.bandwidth_hz = float(self.sample_rate_hz)
                    ok = True
                    self.log_message.emit(
                        f"SoapySDR: requested Fs rejected; using Fs={self.sample_rate_hz/1e6:.1f} MHz instead."
                    )
                    break
                except Exception:
                    continue
            if not ok:
                raise RuntimeError(
                    f"SoapySDR could not set a valid sample rate. Requested Fs={sr/1e6:.1f} MHz. "
                    f"Tried: {[round(x/1e6,2) for x in tried_sr]}. Last error: {e}"
                )

        # Bandwidth (retry by clamping down to <= SR if needed)
        try:
            self._soapy.set_bandwidth(self._dev, self._SOAPY_SDR_RX, self._chan, self.bandwidth_hz)
        except Exception:
            self.bandwidth_hz = float(min(self.bandwidth_hz, self.sample_rate_hz))
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
            # Best-effort safety: disable Bias-T when this worker closes.
            try:
                if soapy is not None and self._dev is not None and bool(self.antenna_power):
                    self._apply_bias_power(False, quiet=True)
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
        """Read exactly nsamps complex64 samples from the active SoapyRX stream."""
        np = self._np
        buff = np.empty(int(nsamps), np.complex64)
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
            break

        if got < nsamps:
            buff[got:] = 0
        return buff

    @staticmethod
    def _next_pow2(n: int) -> int:
        n = int(max(1, n))
        return 1 << (n - 1).bit_length()

    @staticmethod
    def _nearest_pow2_le(n: int, max_pow2: int = 256) -> int:
        n = int(max(1, n))
        p = 1 << (n.bit_length() - 1)
        return int(min(p, max_pow2))

    def _read_block_overlap_decim(self, fft_n: int, decim: int):
        np = self._np
        fft_n = int(fft_n)
        decim = int(max(1, decim))

        raw_n = fft_n * decim
        raw_hop = (fft_n // 2) * decim
        if raw_hop <= 0:
            raw_hop = max(1, raw_n // 2)

        if getattr(self, "_overlap_tail_raw", None) is None or len(getattr(self, "_overlap_tail_raw")) != raw_hop:
            self._overlap_tail_raw = None

        if self._overlap_tail_raw is None:
            raw = self._read_block(raw_n)
        else:
            new = self._read_block(raw_hop)
            raw = np.concatenate((self._overlap_tail_raw, new), axis=0)

        self._overlap_tail_raw = raw[-raw_hop:].copy()

        if decim > 1:
            if decim & (decim - 1) != 0:
                decim = 1
            else:
                x = raw
                d = decim
                while d > 1:
                    x = 0.5 * (x[0::2] + x[1::2])
                    d //= 2
                raw = x

        if len(raw) < fft_n:
            y = np.zeros(fft_n, np.complex64)
            y[-len(raw):] = raw
            raw = y
        elif len(raw) > fft_n:
            raw = raw[-fft_n:]
        return raw

    def _psd_db(self, x, fft_n: Optional[int] = None):
        np = self._np
        n = int(fft_n) if fft_n is not None else int(self.fft_size)
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

    def _fft_freq_axis(self, center_hz: float, fft_n: Optional[int] = None, fs_hz: Optional[float] = None):
        np = self._np
        n = int(fft_n) if fft_n is not None else int(self.fft_size)
        fs = float(fs_hz) if fs_hz is not None else float(self.sample_rate_hz)
        freqs = np.fft.fftshift(np.fft.fftfreq(n, d=1.0 / fs))
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
        try:
            mad = statistics.median([abs(p - median_noise) for p in noise_candidates]) + 1e-9
        except Exception:
            mad = 1.0

        if self._noise_floor is None:
            self._noise_floor = median_noise
            self._noise_mad = mad
        else:
            alpha = 0.1
            self._noise_floor = (1 - alpha) * self._noise_floor + alpha * median_noise
            self._noise_mad = (1 - alpha) * float(getattr(self, "_noise_mad", mad)) + alpha * mad

        self.noise_floor_updated.emit(self._noise_floor)

        thr_db = float(band.get('threshold_db', self.threshold_db))
        use_nf = bool(band.get('use_noise_floor', self.use_local_noise_floor))
        only_above = bool(band.get('only_show_above', self.only_above_threshold))
        hold = float(band.get('hold_time_s', self.min_hold_time_s))

        if use_nf:
            mad = float(getattr(self, "_noise_mad", 1.0))
            abs_threshold = float(self._noise_floor) + (thr_db * mad)
        else:
            abs_threshold = thr_db

        low_hz = float(frame["low_hz"])
        bin_w = float(frame["bin_width_hz"])
        f_factor = self._freq_factor()

        detections: List[Dict[str, Any]] = []
        max_power = None
        max_freq_mhz = None

        now = time.time()
        band_start_hz = float(band.get("start_hz", float("-inf")))
        band_stop_hz = float(band.get("stop_hz", float("inf")))

        for idx in range(len(powers)):
            p_cal = powers[idx]
            p_raw = float(powers_raw[idx])
            center_hz_raw = low_hz + (idx + 0.5) * bin_w
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
                    ht = self._hit_times.get(key)
                    if ht is None:
                        ht = deque()
                        self._hit_times[key] = ht
                    ht.append(now)
                    while ht and (now - ht[0]) > 0.6:
                        ht.popleft()
                    if len(ht) < 2:
                        continue
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
            if only_above:
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

                        self._overlap_tail_raw = None
                        span_hz = max(1.0, float(stop_hz - start_hz))
                        dev_fs = float(self.sample_rate_hz)
                        target_fs = max(span_hz * 1.10, float(self.bin_width_hz) * 8.0)
                        target_fs = min(dev_fs, target_fs)

                        ratio = dev_fs / max(1.0, target_fs)
                        decim = self._nearest_pow2_le(int(ratio), max_pow2=256) if ratio >= 2.0 else 1
                        eff_fs = dev_fs / float(decim)

                        est_n = int(max(256, min(131072, eff_fs / max(1.0, float(self.bin_width_hz)))))
                        fft_n = self._next_pow2(est_n)

                        try:
                            user_n = int(self.fft_size)
                            if user_n > 0:
                                fft_n = user_n
                        except Exception:
                            pass

                        psd_acc = None
                        n_avg = max(1, int(self.avg_frames))
                        for _i in range(n_avg):
                            if not self._running:
                                break
                            x = self._read_block_overlap_decim(fft_n, decim)
                            if len(x) < 8:
                                continue
                            psd = self._psd_db(x, fft_n=fft_n)
                            psd_acc = psd if psd_acc is None else (psd_acc + psd)

                        if psd_acc is None:
                            continue

                        psd_mean = psd_acc / float(n_avg)
                        freqs = self._fft_freq_axis(center_hz, fft_n=fft_n, fs_hz=eff_fs)
                        frame = self._bin_psd_to_band(band, freqs, psd_mean)
                        if frame is not None:
                            self._handle_frame(band, frame)

                        if self.dwell_ms > 0:
                            time.sleep(min(0.05, float(self.dwell_ms) / 1000.0))
                    except Exception as e:
                        self.log_message.emit(
                            f"SoapySDR error (driver={self._soapy_driver}, args='{self.soapy_args}'): {e}"
                        )
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
        self.setWindowTitle("Watchdog")

        # ATAK bridge (runs in background). Window is opened on demand via the top-bar button.
        self.atak_bridge = AtakBridge(self)
        self.atak_window = None
        self.atak_bridge.status_changed.connect(lambda s: self.append_log(f"ATAK: {s}"))

        # Center Frequency (CF) tuner window (popup)
        self.cf_tuner_window = None

        # Parallel mode (multi HackRF) state
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
        self._parallel_update_ui()

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

        # Performance preset UI removed (per-band device settings are authoritative).
        # Keep the combo alive (hidden) for backward references / config migration.
        self.perf_combo = QtWidgets.QComboBox(self)
        self.perf_combo.addItems(list(PERF_PRESETS))
        self.perf_combo.setCurrentText(PERF_BALANCED)
        self.perf_combo.hide()


        self.advanced_toggle = QtWidgets.QToolButton()
        self.advanced_toggle.setText("Advanced")
        self.advanced_toggle.setCheckable(True)
        self.advanced_toggle.setChecked(False)
        self.advanced_toggle.setArrowType(QtCore.Qt.RightArrow)
        self.advanced_toggle.setToolButtonStyle(QtCore.Qt.ToolButtonTextBesideIcon)
        top_bar.addWidget(self.advanced_toggle)

        self.cf_tuner_btn = QtWidgets.QPushButton("CF Tuner")
        top_bar.addWidget(self.cf_tuner_btn)

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
        det_group.setVisible(False)  # UI simplified
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

        # IMPORTANT: The legacy top-right "Device" box is now hidden/removed from
        # the visible layout, but some existing code paths (notably
        # refresh_device_list during __init__) still reference widgets that live
        # inside it (e.g. self.device_type_combo). If we don't parent/retain the
        # group, Qt will destroy it at the end of _build_ui (since it isn't added
        # to a layout), and those references become "wrapped C/C++ object has been
        # deleted" at runtime.
        device_group.setParent(self)
        self._legacy_device_group = device_group
        dev_layout = QtWidgets.QGridLayout(device_group)

        dev_layout.addWidget(QtWidgets.QLabel("Type:"), 0, 0)
        self.device_type_combo = QtWidgets.QComboBox()
        self.device_type_combo.addItems([DEVICE_HACKRF, DEVICE_SOAPY])
        dev_layout.addWidget(self.device_type_combo, 0, 1, 1, 2)

        self.device_select_label = QtWidgets.QLabel("Device:")
        dev_layout.addWidget(self.device_select_label, 1, 0)
        self.device_combo = QtWidgets.QComboBox()
        dev_layout.addWidget(self.device_combo, 1, 1, 1, 2)


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

        # ---------------- Top row layout: Detection (left) ----------------
        # The old top-right Device/Soapy box is superseded by per-band cards.
        # We keep it instantiated (to avoid breaking any references) but do not display it.
        device_group.setVisible(False)
        device_group.setMaximumWidth(0)

        top_row = QtWidgets.QHBoxLayout()
        det_group.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Preferred)
        top_row.addWidget(det_group, 1)
        main_layout.addLayout(top_row)

        # ---------------- Band configuration group ----------------
        # Collapsible per-band "cards" (always-visible header: enable + span + device + start/stop,
        # expandable body: start/stop edits + placeholder for future per-band settings).

        band_group = QtWidgets.QGroupBox("Band configurations")
        bg_v = QtWidgets.QVBoxLayout(band_group)
        bg_v.setContentsMargins(8, 8, 8, 8)
        bg_v.setSpacing(6)

        # Visible top-row controls for the band cards
        band_top = QtWidgets.QWidget()
        band_top_l = QtWidgets.QHBoxLayout(band_top)
        band_top_l.setContentsMargins(0, 0, 0, 0)
        band_top_l.setSpacing(8)

        self.refresh_devices_btn_top = QtWidgets.QPushButton("Refresh devices")
        self.refresh_devices_btn_top.setToolTip("Re-scan connected SDRs (HackRF + SoapySDR)")
        band_top_l.addStretch(1)
        band_top_l.addWidget(self.refresh_devices_btn_top)

        bg_v.addWidget(band_top)

        # Helper to build one band card
        def _build_band_card(letter: str, enable_cb, start_edit, stop_edit, device_combo, start_btn, stop_btn):
            card = QtWidgets.QFrame()
            card.setFrameShape(QtWidgets.QFrame.NoFrame)
            card_v = QtWidgets.QVBoxLayout(card)
            card_v.setContentsMargins(0, 0, 0, 0)
            card_v.setSpacing(2)

            # Header row
            hdr = QtWidgets.QWidget()
            hdr_l = QtWidgets.QHBoxLayout(hdr)
            hdr_l.setContentsMargins(0, 0, 0, 0)
            hdr_l.setSpacing(8)

            exp_btn = QtWidgets.QToolButton()
            exp_btn.setCheckable(True)
            exp_btn.setChecked(False)
            exp_btn.setArrowType(QtCore.Qt.RightArrow)
            exp_btn.setToolButtonStyle(QtCore.Qt.ToolButtonIconOnly)
            exp_btn.setAutoRaise(True)
            exp_btn.setFixedWidth(18)
            # Keep the tiny band expander readable even when global QToolButton
            # styles add padding for larger buttons.
            exp_btn.setStyleSheet(
                """
                QToolButton { padding: 0px; border: 1px solid transparent; min-width: 18px; max-width: 18px; }
                QToolButton:hover { border: 1px solid #666; }
                QToolButton:pressed { padding-top: 1px; padding-bottom: 0px; }
                """
            )

            # Always-visible span label
            span_lbl = QtWidgets.QLabel("Span: ---.---–---.--- MHz")
            span_lbl.setStyleSheet("color: #bfbfbf;")

            status_lbl = QtWidgets.QLabel("Idle")
            status_lbl.setStyleSheet("color: #9a9a9a;")

            # Move the device combo + start/stop buttons into the header (always visible)
            dev_lbl = QtWidgets.QLabel("Device:")

            hdr_l.addWidget(exp_btn)
            hdr_l.addWidget(enable_cb)
            hdr_l.addSpacing(8)
            hdr_l.addWidget(span_lbl, 1)
            hdr_l.addWidget(status_lbl)

            # Quick span presets
            preset_combo = QtWidgets.QComboBox()
            preset_combo.setMaximumWidth(210)
            preset_combo.addItem("Preset…", None)
            preset_combo.addItem("Ham 2m (144–148)", (144.0, 148.0))
            preset_combo.addItem("Ham 70cm (420–450)", (420.0, 450.0))
            preset_combo.addItem("ISM 902–928", (902.0, 928.0))
            preset_combo.addItem("FRS/GMRS (462–467)", (462.0, 467.0))
            preset_combo.addItem("2.4 GHz Wi‑Fi (2400–2483.5)", (2400.0, 2483.5))
            preset_combo.addItem("5.8 GHz ISM (5725–5875)", (5725.0, 5875.0))

            def _apply_preset(_i: int):
                try:
                    v = preset_combo.currentData()
                    if v and isinstance(v, tuple) and len(v) == 2:
                        a, b = float(v[0]), float(v[1])
                        start_edit.blockSignals(True); stop_edit.blockSignals(True)
                        start_edit.setValue(a); stop_edit.setValue(b)
                        start_edit.blockSignals(False); stop_edit.blockSignals(False)
                        _update_span()
                        # enforce device clamps immediately
                        try:
                            _enforce_band_span(letter, "stop")
                            _enforce_band_span(letter, "start")
                        except Exception:
                            pass
                    preset_combo.setCurrentIndex(0)
                except Exception:
                    try:
                        preset_combo.setCurrentIndex(0)
                    except Exception:
                        pass

            preset_combo.currentIndexChanged.connect(_apply_preset)

            hdr_l.addWidget(preset_combo)
            hdr_l.addStretch(1)
            hdr_l.addWidget(dev_lbl)
            hdr_l.addWidget(device_combo)
            hdr_l.addWidget(start_btn)
            hdr_l.addWidget(stop_btn)

            # Details row (collapsible)
            details = QtWidgets.QWidget()
            details_g = QtWidgets.QGridLayout(details)
            details_g.setContentsMargins(26, 2, 0, 6)
            details_g.setHorizontalSpacing(10)
            details_g.setVerticalSpacing(6)

            details_g.addWidget(QtWidgets.QLabel("Start (MHz)"), 0, 0)
            details_g.addWidget(start_edit, 0, 1)
            details_g.addWidget(QtWidgets.QLabel("Stop (MHz)"), 0, 2)
            details_g.addWidget(stop_edit, 0, 3)

            # Per-band controls (initial set: detection threshold + hold time)
            det_box = QtWidgets.QGroupBox("Detection")
            det_g = QtWidgets.QGridLayout(det_box)
            thr_spin = QtWidgets.QDoubleSpinBox()
            thr_spin.setRange(-200.0, 100.0)
            thr_spin.setDecimals(1)
            thr_spin.setSingleStep(0.5)
            thr_spin.setValue(float(self.threshold_spin.value()) if hasattr(self, "threshold_spin") else 3.0)
            hold_spin = QtWidgets.QDoubleSpinBox()
            hold_spin.setRange(0.0, 60.0)
            hold_spin.setDecimals(1)
            hold_spin.setSingleStep(0.1)
            hold_spin.setValue(float(self.persistence_spin.value()) if hasattr(self, "persistence_spin") else 1.5)
            use_noise_cb = QtWidgets.QCheckBox("Use local noise floor")
            use_noise_cb.setChecked(bool(self.use_noise_floor_cb.isChecked()) if hasattr(self, "use_noise_floor_cb") else True)
            only_show_cb = QtWidgets.QCheckBox("Only show detections above threshold")
            only_show_cb.setChecked(bool(self.only_above_threshold_cb.isChecked()) if hasattr(self, "only_above_threshold_cb") else False)
            det_g.addWidget(QtWidgets.QLabel("Threshold (dB)"), 0, 0)
            det_g.addWidget(thr_spin, 0, 1)
            det_g.addWidget(QtWidgets.QLabel("Hold time (s)"), 0, 2)
            det_g.addWidget(hold_spin, 0, 3)
            det_g.addWidget(use_noise_cb, 1, 0, 1, 2)
            det_g.addWidget(only_show_cb, 1, 2, 1, 2)
            details_g.addWidget(det_box, 1, 0, 1, 4)

            # Per-band alarm controls
            alarm_box = QtWidgets.QGroupBox("Alarm")
            alarm_g = QtWidgets.QGridLayout(alarm_box)
            alarm_cb = QtWidgets.QCheckBox("Beep on detection")
            alarm_cb.setChecked(True)
            alarm_combo = QtWidgets.QComboBox()
            # Clone global sound options so per-band stays in sync
            if hasattr(self, "beep_sound_combo"):
                for i in range(self.beep_sound_combo.count()):
                    alarm_combo.addItem(self.beep_sound_combo.itemText(i), self.beep_sound_combo.itemData(i))
                try:
                    alarm_combo.setCurrentIndex(int(self.beep_sound_combo.currentIndex()))
                except Exception:
                    pass
            alarm_g.addWidget(alarm_cb, 0, 0, 1, 2)
            alarm_g.addWidget(QtWidgets.QLabel("Sound"), 1, 0)
            alarm_g.addWidget(alarm_combo, 1, 1)
            details_g.addWidget(alarm_box, 2, 0, 1, 4)

            # Device-specific settings (shown based on selected device)
            dev_box = QtWidgets.QGroupBox("Device settings")

            # Keep device settings panel compact (do not let it consume vertical space).
            try:
                dev_box.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
                dev_box.setMinimumHeight(0)
            except Exception:
                pass
            dev_box_l = QtWidgets.QVBoxLayout(dev_box)
            dev_box.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
            dev_box_l.setContentsMargins(8, 6, 8, 8)

            dev_stack = QtWidgets.QStackedWidget()
            try:
                dev_stack.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
            except Exception:
                pass
            dev_box_l.addWidget(dev_stack)

            dev_blank = QtWidgets.QWidget()
            dev_stack.addWidget(dev_blank)

            def _set_bias_switch_state(sw: QtWidgets.QPushButton, on: bool) -> None:
                sw.blockSignals(True)
                sw.setChecked(bool(on))
                sw.setText("Bias-T ON" if on else "Bias-T OFF")
                sw.blockSignals(False)

            def _make_bias_switch() -> QtWidgets.QPushButton:
                sw = QtWidgets.QPushButton()
                sw.setCheckable(True)
                sw.setCursor(QtCore.Qt.PointingHandCursor)
                sw.setMinimumWidth(110)
                sw.setMaximumWidth(130)
                sw.setToolTip("Bias-T antenna power for this band/device.")
                sw.setStyleSheet(
                    """
                    QPushButton {
                        border-radius: 11px;
                        border: 1px solid #666;
                        background: #4a4a4a;
                        color: #eeeeee;
                        font-weight: 600;
                        padding: 3px 10px;
                    }
                    QPushButton:checked {
                        border: 1px solid #4cc470;
                        background: #1f8a3b;
                        color: #ffffff;
                    }
                    QPushButton:pressed {
                        padding-top: 4px;
                        padding-bottom: 2px;
                    }
                    """
                )
                _set_bias_switch_state(sw, False)
                return sw

            hackrf_bias_switch = _make_bias_switch()
            soapy_bias_switch = _make_bias_switch()

            def _sync_bias_switches(src: QtWidgets.QPushButton, dst: QtWidgets.QPushButton) -> None:
                def _on_toggled(on: bool):
                    src.setText("Bias-T ON" if on else "Bias-T OFF")
                    _set_bias_switch_state(dst, on)
                src.toggled.connect(_on_toggled)

            _sync_bias_switches(hackrf_bias_switch, soapy_bias_switch)
            _sync_bias_switches(soapy_bias_switch, hackrf_bias_switch)

            # --- HackRF page ---
            hackrf_page = QtWidgets.QWidget()
            hg = QtWidgets.QGridLayout(hackrf_page)
            hg.setHorizontalSpacing(10)
            hg.setVerticalSpacing(6)
            hg.setColumnStretch(1, 1)
            hg.setColumnStretch(3, 1)

            hackrf_mode_combo = QtWidgets.QComboBox()
            hackrf_mode_combo.addItem("Sweep (hackrf_sweep)", "sweep")
            hackrf_mode_combo.addItem("Stream (IQ, max 20 MSPS)", "stream")

            hackrf_bin_mode = QtWidgets.QComboBox()
            hackrf_bin_mode.addItem("Auto", "auto")
            hackrf_bin_mode.addItem("Manual", "manual")

            hackrf_bin_spin = QtWidgets.QSpinBox()
            hackrf_bin_spin.setRange(1000, 5000000)
            hackrf_bin_spin.setSingleStep(1000)
            hackrf_bin_spin.setValue(250000)
            hackrf_bin_spin.setMaximumWidth(140)

            hackrf_interval_spin = QtWidgets.QSpinBox()
            hackrf_interval_spin.setRange(1, 10000)
            try:
                hackrf_interval_spin.setValue(int(self.interval_spin.value()))
            except Exception:
                hackrf_interval_spin.setValue(250)
            hackrf_interval_spin.setMaximumWidth(140)

            hackrf_start_delay_spin = QtWidgets.QSpinBox()
            hackrf_start_delay_spin.setRange(0, 10000)
            hackrf_start_delay_spin.setValue(0)
            hackrf_start_delay_spin.setMaximumWidth(140)

            hackrf_gain_spin = QtWidgets.QDoubleSpinBox()
            hackrf_gain_spin.setRange(0.0, 80.0)
            hackrf_gain_spin.setDecimals(1)
            hackrf_gain_spin.setSingleStep(1.0)
            hackrf_gain_spin.setValue(16.0)
            hackrf_gain_spin.setMaximumWidth(140)

            hg.addWidget(QtWidgets.QLabel("Mode"), 0, 0)
            hg.addWidget(hackrf_mode_combo, 0, 1)
            hg.addWidget(QtWidgets.QLabel("Bin width"), 1, 0)
            hg.addWidget(hackrf_bin_mode, 1, 1)
            hg.addWidget(QtWidgets.QLabel("Manual bin (Hz)"), 1, 2)
            hg.addWidget(hackrf_bin_spin, 1, 3)
            hg.addWidget(QtWidgets.QLabel("Interval (ms)"), 2, 0)
            hg.addWidget(hackrf_interval_spin, 2, 1)
            hg.addWidget(QtWidgets.QLabel("Start delay (ms)"), 2, 2)
            hg.addWidget(hackrf_start_delay_spin, 2, 3)
            hg.addWidget(QtWidgets.QLabel("Gain (dB)"), 3, 0)
            hg.addWidget(hackrf_gain_spin, 3, 1)
            hg.addWidget(QtWidgets.QLabel("Bias-T power"), 3, 2)
            hg.addWidget(hackrf_bias_switch, 3, 3)

            dev_stack.addWidget(hackrf_page)

            # --- Soapy page (bladeRF / RTL-SDR / HackRF stream backend) ---
            soapy_page = QtWidgets.QWidget()
            sg = QtWidgets.QGridLayout(soapy_page)
            sg.setHorizontalSpacing(10)
            sg.setVerticalSpacing(6)
            sg.setColumnStretch(1, 1)

            soapy_gain_spin = QtWidgets.QDoubleSpinBox()
            soapy_gain_spin.setRange(0.0, 80.0)
            soapy_gain_spin.setDecimals(1)
            soapy_gain_spin.setSingleStep(1.0)
            soapy_gain_spin.setValue(30.0)
            soapy_gain_spin.setMaximumWidth(140)

            sg.addWidget(QtWidgets.QLabel("Gain (dB)"), 0, 0)
            sg.addWidget(soapy_gain_spin, 0, 1)
            sg.addWidget(QtWidgets.QLabel("Bias-T power"), 0, 2)
            sg.addWidget(soapy_bias_switch, 0, 3)
            dev_stack.addWidget(soapy_page)

            def _update_hackrf_controls():
                is_stream = (str(hackrf_mode_combo.currentData()) == "stream")
                # Start delay is sweep-only
                hackrf_start_delay_spin.setEnabled(not is_stream)
                # Gain is primarily used for stream (sweep uses hackrf_sweep gain implicitly)
                hackrf_gain_spin.setEnabled(is_stream)
                # Manual bin width only when selected
                is_manual = (str(hackrf_bin_mode.currentData()) == "manual")
                hackrf_bin_spin.setEnabled(is_manual)
                try:
                    _fit_device_settings_height()
                except Exception:
                    pass

            hackrf_mode_combo.currentIndexChanged.connect(_update_hackrf_controls)
            hackrf_bin_mode.currentIndexChanged.connect(_update_hackrf_controls)
            _update_hackrf_controls()

            # Ensure span clamping updates immediately when switching HackRF Sweep/Stream.
            try:
                hackrf_mode_combo.currentIndexChanged.connect(lambda _i, b=letter: _enforce_band_span(b, "stop"))
            except Exception:
                pass

            def _fit_device_settings_height():
                """Size device settings panel to the currently visible page content."""
                try:
                    current = dev_stack.currentWidget()
                    if current is None:
                        return
                    page_h = int(max(28, current.sizeHint().height()))
                    # Prevent pathological growth if a style reports an oversized hint.
                    page_h = int(min(page_h + 2, 260))
                    dev_stack.setFixedHeight(page_h)

                    m = dev_box_l.contentsMargins()
                    box_h = int(page_h + m.top() + m.bottom())
                    box_h = int(max(44, min(box_h, 300)))
                    dev_box.setMinimumHeight(box_h)
                    dev_box.setMaximumHeight(box_h)
                    dev_box.updateGeometry()
                except Exception:
                    pass

            def _select_device_page():
                d = device_combo.currentData()
                kind = str(d.get("kind", "auto")) if isinstance(d, dict) else "auto"
                t = (device_combo.currentText() or "").lower()
                # In parallel mode, Auto will pick a HackRF by default
                if kind == "hackrf" or kind == "auto" or "hackrf" in t:
                    dev_stack.setCurrentWidget(hackrf_page)
                elif kind == "soapy" or "soapy" in t or "rtl" in t or "blade" in t:
                    dev_stack.setCurrentWidget(soapy_page)
                else:
                    dev_stack.setCurrentWidget(dev_blank)
                _fit_device_settings_height()

            device_combo.currentIndexChanged.connect(_select_device_page)
            _select_device_page()

            details_g.addWidget(dev_box, 3, 0, 1, 4)
            # Keep the device settings box from expanding vertically by giving the extra
            # space to a spacer row below it.
            try:
                details_g.setRowStretch(3, 0)
                details_g.addItem(QtWidgets.QSpacerItem(0, 0, QtWidgets.QSizePolicy.Minimum, QtWidgets.QSizePolicy.Expanding), 4, 0, 1, 4)
                details_g.setRowStretch(4, 1)
            except Exception:
                pass

            # Tighten dev_box height to the currently selected device page
            try:
                QtCore.QTimer.singleShot(0, _fit_device_settings_height)
            except Exception:
                pass

            extra = {"threshold_spin": thr_spin, "hold_spin": hold_spin, "use_noise_cb": use_noise_cb, "only_show_cb": only_show_cb,
                     "alarm_cb": alarm_cb, "alarm_combo": alarm_combo,
                     "status_lbl": status_lbl, "preset_combo": preset_combo,
                     "hackrf_mode_combo": hackrf_mode_combo, "hackrf_bin_mode": hackrf_bin_mode, "hackrf_bin_spin": hackrf_bin_spin,
                     "hackrf_interval_spin": hackrf_interval_spin, "hackrf_start_delay_spin": hackrf_start_delay_spin,
                     "hackrf_gain_spin": hackrf_gain_spin, "hackrf_bias_switch": hackrf_bias_switch,
                     "soapy_gain_spin": soapy_gain_spin, "soapy_bias_switch": soapy_bias_switch,
                     "bias_switch": hackrf_bias_switch}

            details.setVisible(False)

            def _update_span():
                a = float(start_edit.value())
                b = float(stop_edit.value())
                span_lbl.setText(f"Span: {a:0.6f}–{b:0.6f} MHz")

            start_edit.valueChanged.connect(_update_span)
            stop_edit.valueChanged.connect(_update_span)
            try:
                start_edit.valueChanged.connect(lambda _v, b=letter: _enforce_band_span(b, "start"))
                stop_edit.valueChanged.connect(lambda _v, b=letter: _enforce_band_span(b, "stop"))
            except Exception:
                pass
            _update_span()

            def _toggle(opened: bool):
                details.setVisible(opened)
                exp_btn.setArrowType(QtCore.Qt.DownArrow if opened else QtCore.Qt.RightArrow)

            exp_btn.toggled.connect(_toggle)

            card_v.addWidget(hdr)
            card_v.addWidget(details)

            divider = QtWidgets.QFrame()
            divider.setFrameShape(QtWidgets.QFrame.HLine)
            divider.setFrameShadow(QtWidgets.QFrame.Sunken)
            divider.setStyleSheet("color: #2b2b2b;")

            return card, divider, exp_btn, span_lbl, details, extra

        # Create the same widgets/attributes the backend already expects
        self.bandA_enable = QtWidgets.QCheckBox("Band A")
        self.bandB_enable = QtWidgets.QCheckBox("Band B")
        self.bandC_enable = QtWidgets.QCheckBox("Band C")
        self.bandA_enable.setChecked(True)
        self.bandB_enable.setChecked(True)
        self.bandC_enable.setChecked(True)

        self.bandA_start = QtWidgets.QDoubleSpinBox()
        self.bandA_stop = QtWidgets.QDoubleSpinBox()
        self.bandB_start = QtWidgets.QDoubleSpinBox()
        self.bandB_stop = QtWidgets.QDoubleSpinBox()
        self.bandC_start = QtWidgets.QDoubleSpinBox()
        self.bandC_stop = QtWidgets.QDoubleSpinBox()

        for w in (self.bandA_start, self.bandA_stop, self.bandB_start, self.bandB_stop, self.bandC_start, self.bandC_stop):
            w.setDecimals(6)
            w.setRange(0.0, 7250.0)
            w.setSingleStep(0.001)
            w.setKeyboardTracking(False)

        self.bandA_start.setValue(900.000)
        self.bandA_stop.setValue(930.000)
        self.bandB_start.setValue(144.000)
        self.bandB_stop.setValue(148.000)
        self.bandC_start.setValue(420.000)
        self.bandC_stop.setValue(450.000)

        # Device selectors (populated later by refresh_devices)
        self.bandA_device = QtWidgets.QComboBox()
        self.bandB_device = QtWidgets.QComboBox()
        self.bandC_device = QtWidgets.QComboBox()
        for c in (self.bandA_device, self.bandB_device, self.bandC_device):
            c.setMinimumWidth(240)

        # Per-band Start/Stop buttons (wired up later just like before)
        self.bandA_start_btn = QtWidgets.QPushButton("Start")
        self.bandA_stop_btn = QtWidgets.QPushButton("Stop")
        self.bandB_start_btn = QtWidgets.QPushButton("Start")
        self.bandB_stop_btn = QtWidgets.QPushButton("Stop")
        self.bandC_start_btn = QtWidgets.QPushButton("Start")
        self.bandC_stop_btn = QtWidgets.QPushButton("Stop")
        for b in (self.bandA_stop_btn, self.bandB_stop_btn, self.bandC_stop_btn):
            b.setEnabled(False)


        # -------------------------------------------------------------------
        # Span clamping (UI): keep Start/Stop within the selected device's
        # instantaneous bandwidth. This is intentionally *visible* (spinbox
        # snaps back) so users understand why the span cannot be larger.
        # -------------------------------------------------------------------
        self._last_span_hint_ts = {}  # key: (band, which) -> monotonic seconds

        def _device_max_span_mhz(device_text: str, band_letter: str):
            t = (device_text or "").lower()
            # NOTE: keep these conservative; backend has additional safeguards.
            # HackRF "Stream" mode is clamped to 20 MHz.
            if "hackrf" in t:
                try:
                    ui = self._band_cards.get(str(band_letter), {})
                    mode = str(ui.get("hackrf_mode_combo").currentData()) if ui.get("hackrf_mode_combo") is not None else "sweep"
                    if mode == "stream":
                        return 20.000
                except Exception:
                    pass
                return None  # sweep spans are handled by hackrf_sweep
            if "rtl" in t or "rtlsdr" in t or "rtl-sdr" in t:
                return 2.400
            if "blade" in t:
                return 61.440
            return None  # HackRF sweep, unknown, etc.

        def _show_span_hint(widget: QtWidgets.QWidget, msg: str, band: str):
            # Rate-limit hints so we don't spam while the user scrolls.
            key = (band, id(widget))
            now = time.monotonic()
            last = self._last_span_hint_ts.get(key, 0.0)
            if now - last < 1.0:
                return
            self._last_span_hint_ts[key] = now

            try:
                gpos = widget.mapToGlobal(QtCore.QPoint(0, widget.height()))
                QtWidgets.QToolTip.showText(gpos, msg, widget, widget.rect(), 2500)
            except Exception:
                pass
            # Also log once (same rate-limit)
            try:
                self.append_log(msg)
            except Exception:
                pass

        def _refresh_band_span_label(band: str) -> None:
            """Keep the card header span label in sync with spinbox values."""
            try:
                ui = getattr(self, "_band_cards", {}).get(str(band), {})
                span_lbl = ui.get("span")
                if span_lbl is None:
                    return
                if band == "A":
                    start_spin, stop_spin = self.bandA_start, self.bandA_stop
                elif band == "B":
                    start_spin, stop_spin = self.bandB_start, self.bandB_stop
                else:
                    start_spin, stop_spin = self.bandC_start, self.bandC_stop
                span_lbl.setText(f"Span: {float(start_spin.value()):0.6f}–{float(stop_spin.value()):0.6f} MHz")
            except Exception:
                pass

        def _enforce_band_span(band: str, changed: str):
            # changed: "start" or "stop"
            if band == "A":
                start_spin, stop_spin, dev_combo = self.bandA_start, self.bandA_stop, self.bandA_device
            elif band == "B":
                start_spin, stop_spin, dev_combo = self.bandB_start, self.bandB_stop, self.bandB_device
            else:
                start_spin, stop_spin, dev_combo = self.bandC_start, self.bandC_stop, self.bandC_device

            max_span = _device_max_span_mhz(dev_combo.currentText(), band)
            if not max_span:
                return

            def _sync_soapy_rate_to_span(span_mhz: float) -> None:
                """Keep Soapy sample-rate/BW controls matched to the requested span (visible UX)."""
                try:
                    dev_t = (dev_combo.currentText() or "").lower()
                    # Only sync when this band is assigned to a Soapy-style device.
                    if not ("soapy" in dev_t or "blade" in dev_t or "rtl" in dev_t):
                        return
                    target = float(min(span_mhz, max_span))
                    # Avoid fighting the user while typing; we only call on editingFinished/device change.
                    self.soapy_rate_spin.blockSignals(True)
                    self.soapy_bw_spin.blockSignals(True)
                    self.soapy_rate_spin.setValue(round(target, 3))
                    # Default BW tracks SR for full-span coverage.
                    self.soapy_bw_spin.setValue(round(min(target, float(self.soapy_rate_spin.value())), 3))
                except Exception:
                    pass
                finally:
                    try:
                        self.soapy_rate_spin.blockSignals(False)
                        self.soapy_bw_spin.blockSignals(False)
                    except Exception:
                        pass


            lo = float(start_spin.value())
            hi = float(stop_spin.value())

            # If user crosses them, allow it, but still enforce max span.
            span = abs(hi - lo)
            _sync_soapy_rate_to_span(span)
            if span <= max_span + 1e-9:
                return

            # Snap back the value the user most recently changed.
            if changed == "stop":
                if hi >= lo:
                    new_hi = lo + max_span
                else:
                    new_hi = lo - max_span
                stop_spin.blockSignals(True)
                stop_spin.setValue(round(new_hi, 3))
                stop_spin.blockSignals(False)
                _refresh_band_span_label(band)
                _sync_soapy_rate_to_span(max_span)
                _show_span_hint(
                    stop_spin,
                    f"Band {band}: span clamped to {max_span:.3f} MHz (device limit).",
                    band,
                )
            else:
                if hi >= lo:
                    new_lo = hi - max_span
                else:
                    new_lo = hi + max_span
                start_spin.blockSignals(True)
                start_spin.setValue(round(new_lo, 3))
                start_spin.blockSignals(False)
                _refresh_band_span_label(band)
                _sync_soapy_rate_to_span(max_span)
                _show_span_hint(
                    start_spin,
                    f"Band {band}: span clamped to {max_span:.3f} MHz (device limit).",
                    band,
                )

        # Wire up enforcement for all bands (after edits and when device changes)
        for _band, _start, _stop, _dev in (
            ("A", self.bandA_start, self.bandA_stop, self.bandA_device),
            ("B", self.bandB_start, self.bandB_stop, self.bandB_device),
            ("C", self.bandC_start, self.bandC_stop, self.bandC_device),
        ):
            _start.editingFinished.connect(lambda b=_band: _enforce_band_span(b, "start"))
            _stop.editingFinished.connect(lambda b=_band: _enforce_band_span(b, "stop"))
            # Also clamp while dragging/spinning so it doesn't feel "stuck" until focus changes.
            _start.valueChanged.connect(lambda _v, b=_band: _enforce_band_span(b, "start"))
            _stop.valueChanged.connect(lambda _v, b=_band: _enforce_band_span(b, "stop"))
            _dev.currentIndexChanged.connect(lambda _i, b=_band: _enforce_band_span(b, "stop"))
        # Build cards (A/B/C)
        self._band_cards = {}
        for _letter, _enable, _start, _stop, _dev, _sbtn, _tbtn in (
            ("A", self.bandA_enable, self.bandA_start, self.bandA_stop, self.bandA_device, self.bandA_start_btn, self.bandA_stop_btn),
            ("B", self.bandB_enable, self.bandB_start, self.bandB_stop, self.bandB_device, self.bandB_start_btn, self.bandB_stop_btn),
            ("C", self.bandC_enable, self.bandC_start, self.bandC_stop, self.bandC_device, self.bandC_start_btn, self.bandC_stop_btn),
        ):
            _card, _div, _exp, _span, _details, extra = _build_band_card(_letter, _enable, _start, _stop, _dev, _sbtn, _tbtn)
            bg_v.addWidget(_card)
            bg_v.addWidget(_div)
            self._band_cards[_letter] = {"card": _card, "divider": _div, "expand": _exp, "span": _span, "details": _details, **extra}

        # Legacy global bin width controls (kept hidden for compatibility)
        # The UI used to expose a global "Bin width / Auto / Max bins" row. We now use per-band settings,
        # but some helper code may still reference these attributes. Keep them alive and hidden.
        self._legacy_bin_controls = QtWidgets.QWidget(self)
        self._legacy_bin_controls.hide()
        _bin_l = QtWidgets.QHBoxLayout(self._legacy_bin_controls)
        _bin_l.setContentsMargins(0, 0, 0, 0)
        _bin_l.setSpacing(0)

        self.bin_width_spin = QtWidgets.QSpinBox(self._legacy_bin_controls)
        self.bin_width_spin.setRange(1000, 5_000_000)
        self.bin_width_spin.setValue(250_000)
        self.bin_width_spin.setSingleStep(1000)
        self.bin_width_spin.setKeyboardTracking(False)

        self.auto_bin_checkbox = QtWidgets.QCheckBox("Auto", self._legacy_bin_controls)
        self.auto_bin_checkbox.setChecked(True)

        self.max_bins_spin = QtWidgets.QSpinBox(self._legacy_bin_controls)
        self.max_bins_spin.setRange(100, 20000)
        self.max_bins_spin.setValue(400)
        self.max_bins_spin.setKeyboardTracking(False)


        main_layout.addWidget(band_group)

        # Keep the mapping dictionaries used by the rest of the program
        self._band_buttons = {
            "A": (self.bandA_start_btn, self.bandA_stop_btn),
            "B": (self.bandB_start_btn, self.bandB_stop_btn),
            "C": (self.bandC_start_btn, self.bandC_stop_btn),
        }

        self._band_enable_checks = {"A": self.bandA_enable, "B": self.bandB_enable, "C": self.bandC_enable}
        self._band_start_edits = {"A": self.bandA_start, "B": self.bandB_start, "C": self.bandC_start}
        self._band_stop_edits  = {"A": self.bandA_stop,  "B": self.bandB_stop,  "C": self.bandC_stop}
        self._band_device_combos = {"A": self.bandA_device, "B": self.bandB_device, "C": self.bandC_device}

        # Per-device worker state (parallel mode)
        self._dev_workers = {}     # device_id -> Worker
        self._band_to_device = {}  # band -> device_id
        self._device_to_band = {}  # device_id -> band

        # Per-band activity tracking for UI status line
        self._band_last_activity_ts = {"A": None, "B": None, "C": None}

        self._band_rate_ema = {'A': 0.0, 'B': 0.0, 'C': 0.0}
        self._band_rate_last_t = {'A': time.time(), 'B': time.time(), 'C': time.time()}
        self._band_rate_count = {'A': 0, 'B': 0, 'C': 0}
        self._band_status_timer = QtCore.QTimer(self)
        self._band_status_timer.setInterval(1000)
        self._band_status_timer.timeout.connect(self._update_band_status_age)
        self._band_status_timer.start()

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
        self.table.cellDoubleClicked.connect(self._on_detection_row_activated)
        self.table.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_detection_context_menu)
        self.refresh_devices_btn.clicked.connect(self.refresh_device_list)
        if hasattr(self, 'refresh_devices_btn_top'):
            self.refresh_devices_btn_top.clicked.connect(self.refresh_device_list)
        # Per-band start/stop buttons (only active in Parallel mode)
        self.bandA_start_btn.clicked.connect(lambda: self._parallel_start_band_by_name("A"))
        self.bandA_stop_btn.clicked.connect(lambda: self._parallel_stop_band_by_name("A"))
        self.bandB_start_btn.clicked.connect(lambda: self._parallel_start_band_by_name("B"))
        self.bandB_stop_btn.clicked.connect(lambda: self._parallel_stop_band_by_name("B"))
        self.bandC_start_btn.clicked.connect(lambda: self._parallel_start_band_by_name("C"))
        self.bandC_stop_btn.clicked.connect(lambda: self._parallel_stop_band_by_name("C"))
        self.device_type_combo.currentIndexChanged.connect(self._on_device_type_changed)
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

        self.cf_tuner_btn.clicked.connect(self.show_cf_tuner)
        self.on_auto_bin_toggled(self.auto_bin_checkbox.isChecked())
        self.on_use_noise_floor_toggled(self.use_noise_floor_cb.isChecked())
        self.on_cal_changed()
        self.update_effective_threshold_label()


    def show_atak_bridge(self):
        # Create the window lazily. Closing the window should not stop the bridge.
        if self.atak_window is None:
            self.atak_window = AtakBridgeWindow(self.atak_bridge, parent=self)
            # If the user closes the window, let Qt destroy it; we keep the bridge running.
            try:
                self.atak_window.setAttribute(QtCore.Qt.WA_DeleteOnClose, True)
            except Exception:
                pass
            try:
                self.atak_window.destroyed.connect(lambda *_: setattr(self, "atak_window", None))
            except Exception:
                pass

        self.atak_window.show()
        self.atak_window.raise_()
        self.atak_window.activateWindow()


    def show_cf_tuner(self):
        # Popup window; can be closed and reopened.
        if CenterFrequencyTunerWindow is None:
            QtWidgets.QMessageBox.warning(
                self,
                "CF Tuner",
                "CF tuner module isn't available.\n\n"
                "Make sure hackrf_watchdog/cf_tuner.py exists and required deps are installed."
            )
            return
        try:
            if self.cf_tuner_window is None:
                self.cf_tuner_window = CenterFrequencyTunerWindow(parent=self)
                # When the dialog finishes, drop the reference so it can be recreated cleanly.
                self.cf_tuner_window.finished.connect(lambda _=0: setattr(self, "cf_tuner_window", None))
            self.cf_tuner_window.show()
            self.cf_tuner_window.raise_()
            self.cf_tuner_window.activateWindow()
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "CF Tuner", f"Failed to open CF tuner window:\n\n{e}")

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
            for info in getattr(self, "_dev_workers", {}).values():
                w = info.get("worker") if isinstance(info, dict) else None
                if w is not None and hasattr(w, "max_log_period_s"):
                    w.max_log_period_s = float(period)
        except Exception:
            pass

    def _show_preflight_error(self, title: str, lines: List[str]) -> None:
        clean = [str(x) for x in lines if str(x).strip()]
        for ln in clean:
            self.append_log(f"[preflight] {ln}")
        text = "\n".join(clean[:12])
        if len(clean) > 12:
            text += "\n..."
        QtWidgets.QMessageBox.critical(self, title, text)

    def _preflight_hackrf(self, context: str) -> bool:
        rep = check_hackrf_backend(require_device=True)
        if bool(rep.get("ok")):
            return True
        lines = [f"{context}: HackRF backend is not ready."]
        lines.extend(rep.get("details") or [])
        self._show_preflight_error("HackRF backend not ready", lines)
        return False

    def _soapy_driver_matches(self, requested: str, available: str) -> bool:
        r = (requested or "").strip().lower()
        a = (available or "").strip().lower()
        if not r or not a:
            return False
        if r == a:
            return True
        if "rtl" in r and "rtl" in a:
            return True
        if "bladerf" in r and "bladerf" in a:
            return True
        return False

    def _validate_soapy_target(self, soapy_args: str, devices: List[Dict[str, Any]]) -> Optional[str]:
        args = str(soapy_args or "").strip() or "driver=bladerf"
        kv = _parse_soapy_args(args)
        driver = str(kv.get("driver") or "").strip()
        serial = str(kv.get("serial") or "").strip()

        if not driver:
            return f"Invalid Soapy args '{args}': missing driver=..."

        if "hackrf" in driver.lower():
            return (
                "Soapy assignment with driver=hackrf is not allowed here. "
                "Assign HackRF directly via the HackRF backend instead."
            )

        if not devices:
            return "No Soapy devices were discovered."

        matches: List[Dict[str, Any]] = []
        for dev in devices:
            dd = str(dev.get("driver_lower") or dev.get("driver") or "").strip().lower()
            if self._soapy_driver_matches(driver, dd):
                matches.append(dev)

        if not matches:
            return f"No Soapy device with driver '{driver}' was discovered."

        if serial:
            for dev in matches:
                if str(dev.get("serial") or "").strip() == serial:
                    return None
            return f"Soapy device driver={driver},serial={serial} was not discovered."

        return None

    def _preflight_soapy_runtime(self, context: str, *, require_util: bool = True) -> Optional[Dict[str, Any]]:
        rep = check_soapy_backend(
            require_rtlsdr=False,
            require_bladerf=False,
            require_util=bool(require_util),
        )
        if bool(rep.get("ok")):
            return rep
        lines = [f"{context}: Soapy backend is not ready."]
        lines.extend(rep.get("details") or [])
        self._show_preflight_error("Soapy backend not ready", lines)
        return None

    def _preflight_soapy_for_args(self, context: str, soapy_args: str) -> bool:
        rep = self._preflight_soapy_runtime(context, require_util=True)
        if rep is None:
            return False
        err = self._validate_soapy_target(soapy_args, list(rep.get("devices") or []))
        if err:
            self._show_preflight_error("Soapy device check failed", [f"{context}: {err}"])
            return False
        return True

    def _preflight_parallel_bands(self, bands: List[Dict[str, Any]]) -> bool:
        need_hackrf = False
        need_auto = False
        soapy_args: List[str] = []

        for b in bands:
            assign = b.get("assign")
            kind = str(assign.get("kind")) if isinstance(assign, dict) else "auto"
            if kind == "soapy":
                soapy_args.append(str(assign.get("args") or "").strip())
            elif kind == "hackrf":
                need_hackrf = True
            else:
                need_auto = True

        if need_hackrf and (not self._preflight_hackrf("Parallel mode")):
            return False

        if soapy_args:
            rep = self._preflight_soapy_runtime("Parallel mode", require_util=True)
            if rep is None:
                return False
            devices = list(rep.get("devices") or [])
            for args in soapy_args:
                err = self._validate_soapy_target(args, devices)
                if err:
                    self._show_preflight_error("Soapy device check failed", [f"Parallel mode: {err}"])
                    return False

        if need_auto:
            # Auto assignment may choose from HackRF and/or Soapy devices.
            if not list_hackrf_devices() and not list_soapy_devices():
                self._show_preflight_error(
                    "No devices",
                    [
                        "Parallel mode: one or more bands use Auto assignment, "
                        "but no HackRF or Soapy devices were discovered."
                    ],
                )
                return False

        return True

    def _preflight_parallel_device_start(self, band: str, device_id: str, hackrf_mode: str) -> bool:
        if device_id.startswith("hackrf:"):
            if not self._preflight_hackrf(f"Band {band}"):
                return False
            if str(hackrf_mode).strip().lower() == "stream":
                if self._preflight_soapy_runtime(f"Band {band} HackRF stream mode", require_util=False) is None:
                    return False
            return True

        if device_id.startswith("soapy:"):
            soapy_args = device_id.split(":", 1)[1]
            return self._preflight_soapy_for_args(f"Band {band}", soapy_args)

        self._show_preflight_error("No device", [f"Band {band}: no valid backend device was resolved."])
        return False

    def refresh_device_list(self):
        """Refresh device selectors used by the parallel workflow."""
        dtype = str(self.device_type_combo.currentText())

        self.device_combo.blockSignals(True)
        self.device_combo.clear()

        if dtype == DEVICE_SOAPY:
            self.device_select_label.setText("SoapySDR device:")
            soapy = list_soapy_devices()
            if not soapy:
                self.device_combo.addItem("No SoapySDR devices found", userData=None)
            else:
                for dev in soapy:
                    self.device_combo.addItem(f"{dev['label']}", userData=str(dev.get('args_str') or ''))
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
            label = str(dev.get("label") or args_str)
            opts.append({"label": label, "data": {"kind": "soapy", "args": args_str}})


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

    def _soapy_rate_for_band(self, band_cfg: dict, soapy_args: str) -> float:
        """Pick a sample-rate/BW that covers the band span, clamped to device capability."""
        try:
            span_hz = float(band_cfg.get("stop_hz", 0.0)) - float(band_cfg.get("start_hz", 0.0))
            if span_hz <= 0:
                span_hz = (float(band_cfg.get("stop_mhz", 0.0)) - float(band_cfg.get("start_mhz", 0.0))) * 1e6
        except Exception:
            span_hz = 0.0
        span_hz = max(span_hz, 200e3)

        args_l = (soapy_args or "").lower()
        cap = 20e6
        if "rtlsdr" in args_l or "rtl-sdr" in args_l or "rtl" in args_l:
            cap = 2.4e6
        elif "bladerf" in args_l or "blade" in args_l:
            cap = 61.44e6
        elif "hackrf" in args_l:
            cap = 20e6

        # Must be >= span, but do not exceed device cap.
        return float(min(cap, max(span_hz, 0.0)))

    def _apply_band_assignment_enabled(self) -> None:
        # Keep per-band assignment dropdowns interactive in both modes so users can
        # inspect/prepare device mappings at any time. Running bands are still
        # locked by _parallel_update_ui() to prevent mid-run changes.
        enabled = True
        for cb in self._band_device_widgets():
            cb.setEnabled(enabled)

    def _on_device_type_changed(self, _idx: int = 0) -> None:
        """Show/hide device-specific UI controls.

        Parallel-only runtime: per-band Device dropdowns drive assignment, and
        this selector controls which backend settings are emphasized.
        """
        dtype = str(self.device_type_combo.currentText())
        is_soapy = (dtype == DEVICE_SOAPY)

        # Parallel mode is always active; keep Soapy controls visible so mixed
        # backend assignments (HackRF + Soapy) can be configured.
        show_soapy_controls = True
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
        # Legacy global Bias-T checkbox is retained for compatibility; per-band switches are authoritative.

        if is_soapy:
            self.append_log("SoapySDR selected: running in time-slice mode (experimental).")

        # Keep lists fresh when switching backend
        self.refresh_device_list()

    # ---------------------------------------------------------------------
    # Parallel per-device worker manager (one worker per physical device)
    # ---------------------------------------------------------------------

    def _parallel_is_active(self) -> bool:
        return bool(getattr(self, "_dev_workers", {}))
    def _parallel_update_ui(self) -> None:
        """Update band start/stop buttons and global Start/Stop state."""
        is_parallel = True

        # Band buttons only visible in Parallel mode
        for band, (sbtn, xbtn) in getattr(self, "_band_buttons", {}).items():
            try:
                sbtn.setVisible(is_parallel)
                xbtn.setVisible(is_parallel)
            except Exception:
                pass

        # Update per-band enabled state
        for band in ("A", "B", "C"):
            sbtn, xbtn = self._band_buttons.get(band, (None, None))
            if sbtn is None or xbtn is None:
                continue
            enabled_cb = getattr(self, f"band{band}_enable", None)
            is_enabled = bool(enabled_cb.isChecked()) if enabled_cb is not None else False
            is_running = band in getattr(self, "_band_to_device", {})

            # Status line in band header
            try:
                ui = getattr(self, "_band_cards", {}).get(band, {})
                status_lbl = ui.get("status_lbl")
                if status_lbl is not None:
                    if is_parallel and is_running:
                        device_id = self._band_to_device.get(band, "")
                        info = self._dev_workers.get(device_id, {})
                        kind = str(info.get("kind") or "")
                        # Determine mode
                        mode_txt = "Running"
                        if kind == "hackrf":
                            try:
                                if isinstance(info.get("worker"), SoapyTimeSliceWorker):
                                    mode_txt = "Running — Stream 20 MSPS"
                                else:
                                    mode_txt = "Running — Sweep"
                            except Exception:
                                mode_txt = "Running — HackRF"
                        elif kind == "soapy":
                            mode_txt = "Running — Soapy"
                        status_lbl.setProperty("_base_text", mode_txt)
                    elif is_parallel and is_enabled:
                        status_lbl.setProperty("_base_text", "Ready")
                    else:
                        status_lbl.setProperty("_base_text", "Idle")
            except Exception:
                pass

            # Lock band controls while running (prevents changing device/span mid-run)
            if is_parallel:
                try:
                    dev_cb = getattr(self, f"band{band}_device", None)
                    start_spin = getattr(self, f"band{band}_start", None)
                    stop_spin = getattr(self, f"band{band}_stop", None)
                    if dev_cb is not None:
                        dev_cb.setEnabled(not is_running)
                    if start_spin is not None:
                        start_spin.setEnabled(not is_running)
                    if stop_spin is not None:
                        stop_spin.setEnabled(not is_running)
                except Exception:
                    pass

            # Start enabled when band enabled, not running
            try:
                sbtn.setEnabled(is_parallel and is_enabled and (not is_running))
                xbtn.setEnabled(is_parallel and is_running)
            except Exception:
                pass

        # Global buttons act as Start All / Stop All in Parallel mode
        try:
            if is_parallel:
                self.start_btn.setText("Start all")
                self.stop_btn.setText("Stop all")
                any_enabled = any(getattr(self, f"band{b}_enable").isChecked() for b in ("A","B","C"))
                any_running = bool(getattr(self, "_dev_workers", {}))
                self.start_btn.setEnabled(any_enabled and (not (all(b in self._band_to_device for b in ("A","B","C") if getattr(self, f"band{b}_enable").isChecked()))))
                self.stop_btn.setEnabled(any_running)
            else:
                self.start_btn.setText("Start")
                self.stop_btn.setText("Stop")
        except Exception:
            pass
    
    def _update_band_status_age(self) -> None:
        """Update per-band status label with last-activity age and a simple rate indicator."""
        try:
            now = time.time()
        except Exception:
            return

        # Update per-band frame-rate estimate (EMA of frames per second)
        try:
            for b in ("A", "B", "C"):
                last_t = float(self._band_rate_last_t.get(b, now))
                dt = max(1e-3, now - last_t)
                cnt = int(self._band_rate_count.get(b, 0))
                inst = float(cnt) / dt
                prev = float(self._band_rate_ema.get(b, 0.0))
                alpha = 0.30
                self._band_rate_ema[b] = (1.0 - alpha) * prev + alpha * inst
                self._band_rate_last_t[b] = now
                self._band_rate_count[b] = 0
        except Exception:
            pass

        for band in ("A", "B", "C"):
            ui = getattr(self, "_band_cards", {}).get(band, {})
            lbl = ui.get("status_lbl")
            if lbl is None:
                continue
            base = lbl.property("_base_text") or lbl.text() or ""
            age_ts = getattr(self, "_band_last_activity_ts", {}).get(band)
            if base.startswith("Running") and age_ts:
                try:
                    age = max(0.0, now - float(age_ts))
                    fps = float(getattr(self, "_band_rate_ema", {}).get(band, 0.0) or 0.0)
                    if fps > 0.0:
                        lbl.setText(f"{base}  ({fps:0.1f}/s, age {age:0.1f}s)")
                    else:
                        lbl.setText(f"{base}  (age {age:0.1f}s)")
                except Exception:
                    lbl.setText(base)
            else:
                lbl.setText(str(base))

    def _parallel_band_config_from_ui(self, band: str) -> dict:
        enabled_cb = getattr(self, f"band{band}_enable")
        start_spin = getattr(self, f"band{band}_start")
        stop_spin = getattr(self, f"band{band}_stop")
        dev_cb = getattr(self, f"band{band}_device")
        start_mhz = float(start_spin.value())
        stop_mhz = float(stop_spin.value())
        return {
            "name": band,
            "enabled": bool(enabled_cb.isChecked()),
            "start_mhz": start_mhz,
            "stop_mhz": stop_mhz,
            "start_hz": start_mhz * 1e6,
            "stop_hz": stop_mhz * 1e6,
            "assign": dev_cb.currentData(),
        }
    def _parallel_device_id_from_assign(self, assign: dict) -> str:
        if not isinstance(assign, dict):
            return "auto"
        kind = str(assign.get("kind", "auto"))
        if kind == "hackrf":
            serial = str(assign.get("serial") or "").strip()
            return f"hackrf:{serial}" if serial else "auto"
        if kind == "soapy":
            args = str(assign.get("args") or "").strip()
            return f"soapy:{args}" if args else "auto"
        return "auto"
    def _parallel_auto_device_pool(self) -> List[str]:
        pool: List[str] = []
        for d in list_hackrf_devices():
            s = str(d.get("serial") or "").strip()
            if s:
                pool.append(f"hackrf:{s}")
        for d in list_soapy_devices():
            args = str(d.get("args_str") or "").strip()
            if args:
                pool.append(f"soapy:{args}")
        return pool

    def _parallel_pick_auto_device(self, used_device_ids: set) -> str:
        for did in self._parallel_auto_device_pool():
            if did not in used_device_ids:
                return did
        return ""
    def _parallel_start_band_by_name(self, band: str) -> None:
        """Start a single band in Parallel mode (one worker per device)."""
        cfg = self._parallel_band_config_from_ui(band)
        if not cfg.get("enabled"):
            self.append_log(f"Band {band} is disabled.")
            self._parallel_update_ui()
            return
        if cfg["stop_mhz"] <= cfg["start_mhz"]:
            self.append_log(f"Band {band} has invalid range.")
            self._parallel_update_ui()
            return
        if band in self._band_to_device:
            self._parallel_update_ui()
            return

        self.append_log(f"Band {band} worker starting...")

        ui = getattr(self, "_band_cards", {}).get(band, {})

        # Bin width (per-band)
        try:
            bin_mode = str(ui.get("hackrf_bin_mode").currentData())
        except Exception:
            bin_mode = "auto"
        if bin_mode == "manual":
            try:
                bin_width = int(ui.get("hackrf_bin_spin").value())
            except Exception:
                bin_width = 250000
        else:
            bin_width = int(self.choose_auto_bin_width([cfg], max_bins=400))
        self.current_bin_width = int(bin_width)

        # Per-band detection settings
        thr = float(ui.get("threshold_spin").value()) if ui.get("threshold_spin") is not None else float(self.threshold_spin.value())
        hold = float(ui.get("hold_spin").value()) if ui.get("hold_spin") is not None else float(self.persistence_spin.value())
        use_noise = bool(ui.get("use_noise_cb").isChecked()) if ui.get("use_noise_cb") is not None else bool(self.use_noise_floor_cb.isChecked())
        only_above = bool(ui.get("only_show_cb").isChecked()) if ui.get("only_show_cb") is not None else bool(self.only_above_threshold_cb.isChecked())

        # HackRF per-band backend mode
        try:
            hackrf_mode = str(ui.get("hackrf_mode_combo").currentData())
        except Exception:
            hackrf_mode = "sweep"

        # Safety clamp: HackRF stream mode cannot exceed 20 MHz span
        try:
            if str(hackrf_mode) == "stream":
                span_hz = float(cfg.get("stop_hz", 0.0)) - float(cfg.get("start_hz", 0.0))
                if span_hz > 20e6 + 1.0:
                    cfg["stop_hz"] = float(cfg.get("start_hz", 0.0)) + 20e6
                    cfg["stop_mhz"] = cfg["stop_hz"] / 1e6
                    self.append_log(f"Band {band}: clamped HackRF stream span to 20 MHz.")
        except Exception:
            pass

        # Timing/gain
        try:
            interval_ms = int(ui.get("hackrf_interval_spin").value())
        except Exception:
            interval_ms = int(self.interval_spin.value())
        try:
            start_delay_ms = int(ui.get("hackrf_start_delay_spin").value())
        except Exception:
            start_delay_ms = 0
        try:
            hackrf_gain_db = float(ui.get("hackrf_gain_spin").value())
        except Exception:
            hackrf_gain_db = 16.0
        try:
            soapy_gain_db = float(ui.get("soapy_gain_spin").value())
        except Exception:
            soapy_gain_db = 30.0
        try:
            antenna_power = bool(ui.get("bias_switch").isChecked())
        except Exception:
            antenna_power = bool(self.bias_tee_checkbox.isChecked())

        params = {
            "bin_width_hz": int(bin_width),
            "threshold_db": float(thr),
            "use_local_noise_floor": bool(use_noise),
            "only_above_threshold": bool(only_above),
            "min_hold_time_s": float(hold),
            "interval_ms": int(interval_ms),
            "start_delay_ms": int(start_delay_ms),
            "antenna_power": bool(antenna_power),
            "cal_gain_db": float(self.cal_gain_spin.value()),
            "cal_loss_db": float(self.cal_loss_spin.value()),
            "freq_ppm": float(self.ppm_spin.value()),
            "hackrf_mode": hackrf_mode,
            "hackrf_gain_db": float(hackrf_gain_db),
            "soapy_gain_db": float(soapy_gain_db),
        }

        # Resolve device
        assign = cfg.get("assign")
        device_id = self._parallel_device_id_from_assign(assign)
        used_device_ids = set(self._dev_workers.keys())
        if device_id == "auto":
            device_id = self._parallel_pick_auto_device(used_device_ids)

        if not device_id:
            QtWidgets.QMessageBox.warning(
                self,
                "No free device",
                f"No free device is available for band {band}.\n"
                "Assign a specific device to this band, or stop a running band first.",
            )
            self._parallel_update_ui()
            return

        if not self._preflight_parallel_device_start(band, device_id, hackrf_mode):
            self._parallel_update_ui()
            return

        if device_id in self._dev_workers:
            self.append_log(
                f"Band {band} not started: device already in use ({device_id}). "
                "Choose a different device assignment."
            )
            self._parallel_update_ui()
            return

        ok = self._parallel_start_device_worker(device_id=device_id, band_cfg=cfg, **params)
        if ok:
            self.status_label.setText("Running...")
        self._parallel_update_ui()

    def _parallel_stop_band_by_name(self, band: str) -> None:
        if band not in self._band_to_device:
            self._parallel_update_ui()
            return
        device_id = self._band_to_device.get(band)
        if not device_id:
            self._parallel_update_ui()
            return
        info = self._dev_workers.get(device_id)
        if not info:
            # stale mapping
            self._band_to_device.pop(band, None)
            self._device_to_band.pop(device_id, None)
            self._parallel_update_ui()
            return
        try:
            self.append_log(f"Stopping band {band} ({device_id})...")
            self.status_label.setText("Stopping...")
            info["worker"].stop()
        except Exception:
            pass
        self._parallel_update_ui()
    def _parallel_start_all(self, bands: list, *, bin_width_hz: int, threshold_db: float, use_local_noise_floor: bool,
                           only_above_threshold: bool, min_hold_time_s: float, interval_ms: int, antenna_power: bool,
                           cal_gain_db: float, cal_loss_db: float, freq_ppm: float) -> None:
        """Start all enabled bands in Parallel mode.

        We delegate to the per-band starter, which reads per-band UI settings (mode/bin/interval/etc).
        """
        for b in ("A", "B", "C"):
            try:
                if getattr(self, f"band{b}_enable").isChecked():
                    self._parallel_start_band_by_name(b)
            except Exception:
                pass

    def _parallel_start_device_worker(self, *, device_id: str, band_cfg: dict, bin_width_hz: int, threshold_db: float,
                                     use_local_noise_floor: bool, only_above_threshold: bool, min_hold_time_s: float,
                                     interval_ms: int, start_delay_ms: int, antenna_power: bool, cal_gain_db: float, cal_loss_db: float,
                                     freq_ppm: float, hackrf_mode: str = "sweep", hackrf_gain_db: float = 16.0, soapy_gain_db: float = 30.0) -> bool:
        """Create and start a device-owned worker for one band."""
        if device_id in self._dev_workers:
            return False

        kind = "soapy" if device_id.startswith("soapy:") else "hackrf"
        band = str(band_cfg.get("name") or "")

        thread = QtCore.QThread(self)

        if kind == "hackrf":
            serial = device_id.split(":", 1)[1]
            label = serial

            # Bias-tee per device
            if antenna_power:
                try:
                    set_bias_tee(True, self.append_log, serial=serial)
                except Exception:
                    pass

            mode = (hackrf_mode or "sweep").strip().lower()
            if mode == "stream":
                # HackRF IQ streaming via SoapySDR (clamped to 20 MSPS / 20 MHz span)
                soapy_args = f"driver=hackrf,serial={serial}"
                label = f"HackRF(stream) {serial}"
                span_hz = float(band_cfg.get('stop_hz', 0.0)) - float(band_cfg.get('start_hz', 0.0))
                fs_hz = float(min(20e6, max(1.0e6, span_hz)))

                worker = SoapyTimeSliceWorker(
                    bands=[band_cfg],
                    bin_width_hz=int(bin_width_hz),
                    threshold_db=float(threshold_db),
                    use_local_noise_floor=bool(use_local_noise_floor),
                    only_above_threshold=bool(only_above_threshold),
                    min_hold_time_s=float(min_hold_time_s),
                    interval_ms=int(interval_ms),
                    soapy_args=str(soapy_args),
                    sample_rate_hz=float(fs_hz),
                    bandwidth_hz=float(fs_hz),
                    gain_db=float(hackrf_gain_db),
                    dwell_ms=250,
                    settle_ms=40,
                    fft_size=int(self.soapy_fft_combo.currentData() or 4096),
                    avg_frames=int(self.soapy_avg_spin.value()),
                    cal_gain_db=float(cal_gain_db),
                    cal_loss_db=float(cal_loss_db),
                    freq_ppm=float(freq_ppm),
                    antenna_power=bool(antenna_power),
                    source_id=label,
                )
                worker.log_message.connect(lambda msg, l=label: self.append_log(f"[{l}] {msg}"))
                worker.noise_floor_updated.connect(lambda v, l=label: self._on_parallel_noise_floor(v, l))
            else:
                # HackRF sweep backend
                worker = SweepWorker(
                    bands=[band_cfg],
                    bin_width_hz=int(bin_width_hz),
                    threshold_db=float(threshold_db),
                    use_local_noise_floor=bool(use_local_noise_floor),
                    only_above_threshold=bool(only_above_threshold),
                    min_hold_time_s=float(min_hold_time_s),
                    interval_ms=int(interval_ms),
                    start_delay_ms=int(start_delay_ms),
                    device_arg=serial,
                    antenna_power=bool(antenna_power),
                    cal_gain_db=float(cal_gain_db),
                    cal_loss_db=float(cal_loss_db),
                    freq_ppm=float(freq_ppm),
                    source_id=label,
                )
                try:
                    worker.max_log_period_s = float(self._current_max_log_period_s())
                except Exception:
                    pass
                worker.log_message.connect(lambda msg, l=label: self.append_log(f"[{l}] {msg}"))
                worker.noise_floor_updated.connect(lambda v, l=label: self._on_parallel_noise_floor(v, l))

        else:
            args = device_id.split(":", 1)[1]
            soapy_kv = _parse_soapy_args(args)
            if "hackrf" in str(soapy_kv.get("driver") or "").lower():
                self.append_log(
                    f"Refusing Soapy driver=hackrf assignment for band {band}. "
                    "Use native HackRF backend assignment."
                )
                return False
            label = f"Soapy:{args}"
            worker = SoapyTimeSliceWorker(
                bands=[band_cfg],
                bin_width_hz=int(bin_width_hz),
                threshold_db=float(threshold_db),
                use_local_noise_floor=bool(use_local_noise_floor),
                only_above_threshold=bool(only_above_threshold),
                min_hold_time_s=float(min_hold_time_s),
                interval_ms=int(interval_ms),
                soapy_args=str(args),
                sample_rate_hz=float(self._soapy_rate_for_band(band_cfg, args)),
                bandwidth_hz=float(self._soapy_rate_for_band(band_cfg, args)),
                gain_db=float(soapy_gain_db),
                dwell_ms=int(self.soapy_dwell_spin.value()),
                settle_ms=int(self.soapy_settle_spin.value()),
                fft_size=int(self.soapy_fft_combo.currentData() or 4096),
                avg_frames=int(self.soapy_avg_spin.value()),
                cal_gain_db=float(cal_gain_db),
                cal_loss_db=float(cal_loss_db),
                freq_ppm=float(freq_ppm),
                antenna_power=bool(antenna_power),
                source_id=label,
            )
            worker.log_message.connect(self.append_log)
            worker.noise_floor_updated.connect(lambda v, l=label: self._on_parallel_noise_floor(v, l))

        worker.detections_found.connect(self.on_detections_found)

        worker.moveToThread(thread)
        thread.started.connect(worker.run)

        # Finish handling
        worker.finished.connect(lambda did=device_id: self._parallel_on_device_finished(did))
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)

        # Register
        self._dev_workers[device_id] = {
            "thread": thread,
            "worker": worker,
            "kind": kind,
            "band": band,
            "label": label,
            "antenna_power": bool(antenna_power),
        }
        self._band_to_device[band] = device_id
        self._device_to_band[device_id] = band

        thread.start()
        self.append_log(f"Band {band} worker started")
        self.append_log(f"Band {band} assigned device: {device_id}")
        return True

    def _parallel_on_device_finished(self, device_id: str) -> None:
        info = self._dev_workers.pop(device_id, None)
        band = self._device_to_band.pop(device_id, None)
        if band:
            self._band_to_device.pop(band, None)

        # Disable bias tee on HackRF as soon as it's free
        try:
            if info and info.get("kind") == "hackrf" and bool(info.get("antenna_power")):
                serial = device_id.split(":", 1)[1]
                set_bias_tee(False, self.append_log, serial=serial)
        except Exception:
            pass

        # If no devices remain, fully return to idle
        if not self._dev_workers:
            self.bias_tee_requested = False
            self.bias_tee_engaged = False
            self.status_label.setText("Idle")
            self.start_btn.setEnabled(True)
            self.stop_btn.setEnabled(False)

        self._parallel_update_ui()

    def _parallel_stop_all(self) -> None:
        if not self._dev_workers:
            self.status_label.setText("Idle")
            self._parallel_update_ui()
            return

        self.append_log("Stopping all running bands...")
        self.status_label.setText("Stopping...")
        for device_id, info in list(self._dev_workers.items()):
            _ = device_id
            try:
                w = info.get("worker") if isinstance(info, dict) else None
                if w is not None:
                    w.stop()
            except Exception:
                pass
    def _on_parallel_noise_floor(self, value: float, label: str) -> None:
        try:
            now_ts = time.time()
            for _did, _info in getattr(self, "_dev_workers", {}).items():
                if str(_info.get("label")) == str(label):
                    _band = str(_info.get("band") or "")
                    if _band:
                        self._band_last_activity_ts[_band] = now_ts
                        try:
                            self._band_rate_count[_band] = self._band_rate_count.get(_band, 0) + 1
                        except Exception:
                            pass
        except Exception:
            pass
        self._noise_by_source[str(label)] = float(value)
        vals = list(self._noise_by_source.values())
        if vals:
            self.on_noise_floor_updated(sum(vals) / float(len(vals)))

    def _load_settings(self) -> None:
        s = QtCore.QSettings()
        # Dark mode default: ON
        try:
            dm = s.value("watchdog/dark_mode")
            if dm is None:
                dm_val = True
            elif isinstance(dm, str):
                dm_val = dm.strip().lower() in ("1", "true", "yes", "on")
            else:
                dm_val = bool(dm)
            self.dark_mode_checkbox.setChecked(dm_val)
        except Exception:
            self.dark_mode_checkbox.setChecked(True)

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

            def _to_bool(v) -> bool:
                if isinstance(v, str):
                    return v.strip().lower() in ("1", "true", "yes", "on")
                return bool(v)

            for band in ("A", "B", "C"):
                card = self._band_cards.get(band, {})
                sw = card.get("bias_switch")
                if sw is None:
                    continue
                v = s.value(f"watchdog/band_bias_{band}", False)
                sw.setChecked(_to_bool(v))
        except Exception:
            pass

        self._on_device_type_changed(0)
    def _save_settings(self) -> None:
        try:
            s = QtCore.QSettings()
            s.setValue("watchdog/dark_mode", bool(self.dark_mode_checkbox.isChecked()))
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
            for band in ("A", "B", "C"):
                card = self._band_cards.get(band, {})
                sw = card.get("bias_switch")
                if sw is not None:
                    s.setValue(f"watchdog/band_bias_{band}", bool(sw.isChecked()))
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
        # Ensure all workers/threads are stopped before window is destroyed to avoid:
        # "QThread: Destroyed while thread is still running"
        try:
            self.stop_watchdog()
        except Exception:
            pass

        # Best-effort join of any remaining per-device threads
        try:
            for info in list(getattr(self, "_dev_workers", {}).values()):
                th = info.get("thread") if isinstance(info, dict) else None
                if th is None:
                    continue
                try:
                    th.quit()
                except Exception:
                    pass
                try:
                    th.wait(1500)
                except Exception:
                    pass
                try:
                    if hasattr(th, "isRunning") and th.isRunning():
                        try:
                            th.terminate()
                        except Exception:
                            pass
                        try:
                            th.wait(800)
                        except Exception:
                            pass
                except Exception:
                    pass
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
        self.update_effective_threshold_label()

    def on_ppm_changed(self, value: float):
        _ = value

    def on_use_noise_floor_toggled(self, checked: bool):
        _ = checked
        self.update_effective_threshold_label()

    def on_threshold_changed(self, value: float):
        _ = value
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

    def choose_auto_bin_width(self, bands: List[Dict[str, Any]], max_bins: int = 400) -> int:
        max_bins = int(max_bins) if max_bins else 400
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


    def _play_alarm_mode(self, mode: str):
        """Play one alarm sound by mode key (system/soft_ding/short_chirp/alarm)."""
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

    def play_alarm_sound(self):
        """Global alarm (legacy/global controls)."""
        if not self.beep_checkbox.isChecked():
            return
        self._play_alarm_mode(self.beep_sound_combo.currentData())

    def play_alarm_sound_for_band(self, band: str):
        """Per-band alarm: uses band card settings when present; falls back to global."""
        card = self._band_cards.get(str(band), {}) if hasattr(self, "_band_cards") else {}
        if card and "alarm_cb" in card and "alarm_combo" in card:
            try:
                if card["alarm_cb"].isChecked():
                    self._play_alarm_mode(card["alarm_combo"].currentData())
                    return
            except Exception:
                pass
        self.play_alarm_sound()


    def start_watchdog(self):
        # Parallel-only runtime: Start means "Start all enabled bands",
        # including bands not currently running.
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
            card = self._band_cards.get(name, {}) if hasattr(self, "_band_cards") else {}
            bands.append(
                {
                    "name": name,
                    "enabled": True,
                    "start_mhz": start_mhz,
                    "stop_mhz": stop_mhz,
                    "start_hz": start_mhz * 1e6,
                    "stop_hz": stop_mhz * 1e6,
                    # Per-band detection overrides from band card (authoritative)
                    "threshold_db": float(card["threshold_spin"].value()) if "threshold_spin" in card else None,
                    "hold_time_s": float(card["hold_spin"].value()) if "hold_spin" in card else None,
                    "use_noise_floor": bool(card["use_noise_cb"].isChecked()) if "use_noise_cb" in card else None,
                    "only_show_above": bool(card["only_show_cb"].isChecked()) if "only_show_cb" in card else None,
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

        antenna_power = self.bias_tee_checkbox.isChecked()
        cal_gain = float(self.cal_gain_spin.value())
        cal_loss = float(self.cal_loss_spin.value())
        ppm = float(self.ppm_spin.value())

        # Keep existing detections if we're already running (Start All can start missing bands).
        if not self._parallel_is_active():
            self.detections.clear()

        self.status_label.setText("Sweeping...")
        # Attach per-band assignment metadata
        assign_map = {'A': self.bandA_device, 'B': self.bandB_device, 'C': self.bandC_device}
        for b in bands:
            cb = assign_map.get(str(b.get('name')))
            if cb is not None:
                b['assign'] = cb.currentData()
        if not self._preflight_parallel_bands(bands):
            self.status_label.setText("Idle")
            self.start_btn.setEnabled(True)
            self.stop_btn.setEnabled(False)
            return

        self._parallel_start_all(
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

    def stop_watchdog(self):
        # Parallel per-device stop
        if self._parallel_is_active():
            self._parallel_stop_all()
            return
        self.status_label.setText("Idle")

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

        # Per-band alarm routing (falls back to global)
        if detections:
            # Backend activity marker (used by per-band status line)
            try:
                now_ts = time.time()
                for _d in detections:
                    _b = _d.get("band")
                    if _b is not None:
                        _b = str(_b).strip()
                        if _b:
                            self._band_last_activity_ts[_b] = now_ts
            except Exception:
                pass

            bands = []
            for d in detections:
                b = d.get("band")
                if b is None:
                    continue
                b = str(b).strip()
                if b:
                    bands.append(b)
            if bands:
                for b in sorted(set(bands)):
                    self.play_alarm_sound_for_band(b)
            else:
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
                QPushButton, QToolButton {
                    background-color: #444;
                    color: #eee;
                    border: 1px solid #666;
                    padding: 4px 8px;
                    border-radius: 3px;
                }
                QPushButton:hover, QToolButton:hover { background-color: #505050; }
                QPushButton:pressed, QToolButton:pressed {
                    background-color: #2f2f2f;
                    border: 1px solid #8a8a8a;
                    padding-top: 5px;
                    padding-bottom: 3px;
                }
                QPushButton:disabled { background-color: #333; color: #777; }
                """
            )
        else:
            # Force a consistent LIGHT theme even if the desktop environment is in dark mode.
            self.setStyleSheet(
                """
                QWidget { background-color: #f3f3f3; color: #111; }
                QGroupBox { border: 1px solid #c8c8c8; margin-top: 6px; }
                QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 3px 0 3px; }
                QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QTextEdit, QTableWidget {
                    background-color: #ffffff; color: #111; border: 1px solid #bdbdbd;
                }
                QHeaderView::section { background-color: #e9e9e9; color: #111; border: 1px solid #cfcfcf; }
                QPushButton, QToolButton {
                    background-color: #e9e9e9;
                    color: #111;
                    border: 1px solid #bdbdbd;
                    padding: 4px 8px;
                    border-radius: 3px;
                }
                QPushButton:hover, QToolButton:hover { background-color: #e1e1e1; }
                QPushButton:pressed, QToolButton:pressed {
                    background-color: #d3d3d3;
                    border: 1px solid #9f9f9f;
                    padding-top: 5px;
                    padding-bottom: 3px;
                }
                QPushButton:disabled, QToolButton:disabled { background-color: #efefef; color: #999; }
                QCheckBox { color: #111; }
                """
            )




    def _get_busy_devices_for_cf_tuner(self):
        """Return (busy_hackrf_serials, busy_soapy_args) currently used by scanning."""
        busy_hackrf = set()
        busy_soapy = set()

        for info in getattr(self, "_dev_workers", {}).values():
            w = info.get("worker") if isinstance(info, dict) else None
            if w is None:
                continue
            try:
                if isinstance(w, SweepWorker):
                    serial = str(getattr(w, "device_arg", "") or getattr(w, "source_id", "") or "").strip()
                    if serial:
                        busy_hackrf.add(serial)
                elif isinstance(w, SoapyTimeSliceWorker):
                    args = str(getattr(w, "soapy_args", "") or "").strip()
                    if args:
                        busy_soapy.add(args)
            except Exception:
                pass

        return busy_hackrf, busy_soapy

    def _open_cf_tuner_with_freq(self, freq_hz: int):
        """Open CF tuner window and preset CF (no auto-start)."""
        self.show_cf_tuner()
        try:
            w = getattr(self, "cf_tuner_window", None)
            if w is None:
                return
            busy_h, busy_s = self._get_busy_devices_for_cf_tuner()
            if hasattr(w, "set_busy_devices"):
                w.set_busy_devices(busy_h, busy_s)
            if hasattr(w, "refresh_devices"):
                w.refresh_devices()
            if hasattr(w, "auto_select_free_device"):
                w.auto_select_free_device()
            if hasattr(w, "set_frequency_hz"):
                w.set_frequency_hz(int(freq_hz))
        except Exception as e:
            try:
                QtWidgets.QMessageBox.warning(self, "CF Tuner", "Could not preset CF tuner:\n\n" + str(e))
            except Exception:
                pass

    def _on_detection_row_activated(self, row: int, _col: int):
        """Double-click a detection row to open CF tuner and preset CF."""
        try:
            item = self.table.item(row, 0)
            if item is None:
                return
            txt = str(item.text() or "").strip()
            if not txt:
                return
            mhz = float(txt)
            self._open_cf_tuner_with_freq(int(mhz * 1_000_000))
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "CF Tuner", "Couldn't tune to detection:\n\n" + str(e))

    def _on_detection_context_menu(self, pos):
        """Right-click menu on detections table."""
        item = self.table.itemAt(pos)
        if item is None:
            return
        row = item.row()

        menu = QtWidgets.QMenu(self)
        act_tune = menu.addAction("Tune in CF Tuner")
        act = menu.exec_(self.table.viewport().mapToGlobal(pos))
        if act == act_tune:
            self._on_detection_row_activated(row, 0)

def main():
    try:
        app = QtWidgets.QApplication(sys.argv)
        app.setStyle("Fusion")
        app.setOrganizationName("Watchdog")
        app.setApplicationName("Watchdog")

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

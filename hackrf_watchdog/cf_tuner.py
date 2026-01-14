# hackrf_watchdog/cf_tuner.py
"""
Center Frequency (CF) Tuner popup window for Watchdog.

- Quick "listen here" on a single SDR without leaving Watchdog.
- Minimal UI: device dropdown, args line, CF spinbox, start/stop, volume, mute.
- Fail-safe: if the device is busy/in use by scanning, show a clear popup.

Implementation:
- Uses SoapySDR runtime via ctypes (no python SoapySDR module required).
- Enumerates devices via SoapySDRUtil --find (like Watchdog).
- Also lists HackRF serials from hackrf_info as optional "driver=hackrf,serial=..." entries
  (requires SoapyHackRF module to actually open; scanning still uses native hackrf_sweep).

Note: Demod is simple NFM (quadrature discriminator) intended for quick checking, not "hi-fi".
"""

import os
import re
import time
import shutil
import ctypes
import ctypes.util
from typing import Any, Dict, List, Optional

from PyQt5 import QtCore, QtWidgets, QtGui
from PyQt5.QtMultimedia import QAudioFormat, QAudioOutput


class GroupedHzSpinBox(QtWidgets.QDoubleSpinBox):
    """
    Integer-Hz spinbox that displays/accepts grouped digits with dots:
      915000000   -> 915.000.000
      6000000000  -> 6.000.000.000
    Internally still uses Hz (integer).
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setDecimals(0)
        self.setRange(1, 10_000_000_000)  # up to 10 GHz
        self.setSingleStep(12_500)
        self.setAccelerated(True)

    def textFromValue(self, value: float) -> str:
        n = int(round(value))

        # SDR++-ish 9 digits for < 1 GHz
        if n < 1_000_000_000:
            s = f"{n:09d}"
            return f"{s[0:3]}.{s[3:6]}.{s[6:9]}"

        # >= 1 GHz: group every 3 digits from the right
        s = str(n)
        parts = []
        while s:
            parts.append(s[-3:])
            s = s[:-3]
        return ".".join(reversed(parts))

    def valueFromText(self, text: str) -> float:
        t = text.strip().replace(".", "").replace(" ", "")
        if not t:
            return 0.0
        return float(int(t))

    def validate(self, text: str, pos: int):
        # Allow digits, dots, spaces while typing
        for c in text:
            if c not in "0123456789. ":
                return (QtGui.QValidator.Invalid, text, pos)

        t = text.strip().replace(".", "").replace(" ", "")
        if t == "":
            return (QtGui.QValidator.Intermediate, text, pos)

        try:
            n = int(t)
        except Exception:
            return (QtGui.QValidator.Intermediate, text, pos)

        if n < int(self.minimum()) or n > int(self.maximum()):
            return (QtGui.QValidator.Intermediate, text, pos)

        return (QtGui.QValidator.Acceptable, text, pos)

# ---------------------------
# Process helpers
# ---------------------------

def _run_text(cmd: List[str], timeout: float = 7.0) -> str:
    import subprocess
    try:
        p = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        return p.stdout or ""
    except Exception:
        return ""


# ---------------------------
# Device discovery
# ---------------------------

def list_hackrf_serials() -> List[str]:
    out = _run_text(["hackrf_info"], timeout=5.0)
    serials: List[str] = []
    for line in out.splitlines():
        raw = line.strip()
        low = raw.lower()
        if "serial" in low and ":" in raw:
            _, val = raw.split(":", 1)
            s = val.strip()
            if s:
                serials.append(s)
    return serials


def _find_soapysdrutil() -> Optional[str]:
    util = shutil.which("SoapySDRUtil")
    if util:
        return util
    if os.name == "nt":
        roots: List[str] = []
        env_root = os.environ.get("POTHOS")
        if env_root:
            roots.append(env_root)
        roots.extend([r"C:\Program Files\PothosSDR", r"C:\Program Files (x86)\PothosSDR"])
        for root in roots:
            cand = os.path.join(root, "bin", "SoapySDRUtil.exe")
            if os.path.isfile(cand):
                return cand
    return None


def list_soapy_devices_allow_all() -> List[Dict[str, Any]]:
    """
    Enumerate Soapy devices via SoapySDRUtil --find.
    Returns a list of dicts with: label, args_str, info
    """
    util = _find_soapysdrutil()
    if not util:
        return []

    out = _run_text([util, "--find"], timeout=7.0)

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
        serial = str(info.get("serial") or "").strip()
        label = (str(info.get("label") or "").strip()
                 or str(info.get("name") or "").strip()
                 or driver)

        args = f"driver={driver}"
        if serial:
            args += f",serial={serial}"

        devices.append({"label": label, "args_str": args, "info": info})
    return devices


# ---------------------------
# SoapySDR C-API wrapper (ctypes)
# ---------------------------

class _SoapyCAPI:
    SOAPY_SDR_RX = 1

    def __init__(self):
        self.lib = self._load_library()
        self._bind()

    @staticmethod
    def _load_library():
        if os.name == "nt":
            roots: List[str] = []
            env_root = os.environ.get("POTHOS")
            if env_root:
                roots.append(env_root)
            roots.extend([r"C:\Program Files\PothosSDR", r"C:\Program Files (x86)\PothosSDR"])
            for root in roots:
                dll_path = os.path.join(root, "bin", "SoapySDR.dll")
                if os.path.isfile(dll_path):
                    try:
                        os.add_dll_directory(os.path.dirname(dll_path))
                    except Exception:
                        os.environ["PATH"] = os.path.dirname(dll_path) + os.pathsep + os.environ.get("PATH", "")
                    return ctypes.WinDLL(dll_path)
            return ctypes.WinDLL("SoapySDR.dll")

        name = ctypes.util.find_library("SoapySDR")
        if name:
            return ctypes.CDLL(name)

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

        self._lastError = lib.SoapySDRDevice_lastError
        self._lastError.restype = c_char_p
        self._lastError.argtypes = []

        self._makeStrArgs = lib.SoapySDRDevice_makeStrArgs
        self._makeStrArgs.restype = c_void_p
        self._makeStrArgs.argtypes = [c_char_p]

        self._unmake = lib.SoapySDRDevice_unmake
        self._unmake.restype = c_int
        self._unmake.argtypes = [c_void_p]

        self._setSampleRate = lib.SoapySDRDevice_setSampleRate
        self._setSampleRate.restype = c_int
        self._setSampleRate.argtypes = [c_void_p, c_int, c_size_t, c_double]

        self._setBandwidth = lib.SoapySDRDevice_setBandwidth
        self._setBandwidth.restype = c_int
        self._setBandwidth.argtypes = [c_void_p, c_int, c_size_t, c_double]

        self._setFrequency = lib.SoapySDRDevice_setFrequency
        self._setFrequency.restype = c_int
        self._setFrequency.argtypes = [c_void_p, c_int, c_size_t, c_double, c_void_p]

        self._setGain = lib.SoapySDRDevice_setGain
        self._setGain.restype = c_int
        self._setGain.argtypes = [c_void_p, c_int, c_size_t, c_double]

        self._setGainElement = getattr(lib, "SoapySDRDevice_setGainElement", None)
        if self._setGainElement is not None:
            self._setGainElement.restype = c_int
            self._setGainElement.argtypes = [c_void_p, c_int, c_size_t, c_char_p, c_double]

        # Optional device settings + AGC control (used for RTL-SDR options)
        self._writeSetting = getattr(lib, "SoapySDRDevice_writeSetting", None)
        if self._writeSetting is not None:
            self._writeSetting.restype = None
            self._writeSetting.argtypes = [c_void_p, c_char_p, c_char_p]

        self._setGainMode = getattr(lib, "SoapySDRDevice_setGainMode", None)
        if self._setGainMode is not None:
            self._setGainMode.restype = c_int
            self._setGainMode.argtypes = [c_void_p, c_int, c_size_t, ctypes.c_bool]

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
            c_void_p,
            c_void_p,
            ctypes.POINTER(c_void_p),
            c_size_t,
            ctypes.POINTER(c_int),
            ctypes.POINTER(c_longlong),
            c_long,
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
        self._setFrequency(dev, int(direction), ctypes.c_size_t(chan), float(freq_hz), ctypes.c_void_p(0))

    def set_gain(self, dev, direction: int, chan: int, gain_db: float):
        self._setGain(dev, int(direction), ctypes.c_size_t(chan), float(gain_db))


    def set_gain_element(self, dev, direction: int, chan: int, name: str, gain_db: float):
        if getattr(self, "_setGainElement", None) is None:
            raise AttributeError("SoapySDRDevice_setGainElement not available")
        self._setGainElement(dev, int(direction), ctypes.c_size_t(chan), str(name).encode("utf-8"), float(gain_db))


    def set_gain_mode(self, dev, direction: int, chan: int, enable: bool):
        """Enable/disable tuner AGC if the driver supports it."""
        if getattr(self, "_setGainMode", None) is None:
            return
        self._setGainMode(dev, int(direction), ctypes.c_size_t(chan), bool(enable))

    def write_setting(self, dev, key: str, value: str):
        """Write a driver-specific setting if supported (e.g. RTL-SDR offset_tune)."""
        fn = getattr(self, "_writeSetting", None)
        if fn is None:
            return
        fn(dev, str(key).encode("utf-8"), str(value).encode("utf-8"))

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
        ret = self._readStream(
            dev,
            stream,
            buff_ptr,
            ctypes.c_size_t(num_elems),
            ctypes.byref(flags),
            ctypes.byref(timeNs),
            ctypes.c_long(int(timeout_us)),
        )
        return int(ret)


# ---------------------------
# RX -> Audio thread
# ---------------------------

class _RxAudioThread(QtCore.QThread):
    audio_bytes = QtCore.pyqtSignal(bytes)
    error = QtCore.pyqtSignal(str)
    status = QtCore.pyqtSignal(str)

    def __init__(self, *, soapy_args: str, device_driver: str, center_hz: int, sample_rate_hz: int, gain_db: float, demod_mode: str,
                 rtl_settings: Optional[Dict[str, str]] = None, rtl_tuner_agc: bool = False,
                 hackrf_lna_db: float = 32.0, hackrf_vga_db: float = 20.0, hackrf_amp: bool = True, hackrf_bias_tx: bool = False,
                 bladerf_agc: bool = False, bladerf_biastee_rx: bool = False,
                 parent=None):
        super().__init__(parent)
        self.soapy_args = str(soapy_args)
        self.device_driver = str(device_driver or "").strip().lower()
        self.sample_rate_hz = int(sample_rate_hz)
        self.gain_db = float(gain_db)
        self.demod_mode = str(demod_mode or "Auto")

        # RTL-SDR
        self.rtl_settings = dict(rtl_settings or {})
        self.rtl_tuner_agc = bool(rtl_tuner_agc)

        # HackRF gain staging
        self.hackrf_lna_db = float(hackrf_lna_db)
        self.hackrf_vga_db = float(hackrf_vga_db)
        self.hackrf_amp = bool(hackrf_amp)
        self.hackrf_bias_tx = bool(hackrf_bias_tx)

        # bladeRF
        self.bladerf_agc = bool(bladerf_agc)
        self.bladerf_biastee_rx = bool(bladerf_biastee_rx)

        self._stop = False
        self._cf_hz = int(center_hz)

    def stop(self):
        self._stop = True

    def set_center_freq(self, cf_hz: int):
        self._cf_hz = int(cf_hz)

    def run(self):
        try:
            try:
                import numpy as np  # type: ignore
            except Exception as e:
                self.error.emit(f"CF tuner requires NumPy.\n\nImport error: {e}")
                return

            self.status.emit("Loading SoapySDR runtime...")
            soapy = _SoapyCAPI()

            self.status.emit(f"Opening device: {self.soapy_args}")
            dev = soapy.make(self.soapy_args)
            stream = None
            try:
                chan = 0

                drv = (self.device_driver or "").strip().lower()

                # Apply device-specific options BEFORE streaming starts.
                if drv == "rtlsdr":
                    # RTL-SDR: apply driver settings + optional tuner AGC.
                    if self.rtl_settings:
                        for k, v in self.rtl_settings.items():
                            try:
                                soapy.write_setting(dev, k, v)
                            except Exception:
                                pass
                    try:
                        soapy.set_gain_mode(dev, soapy.SOAPY_SDR_RX, chan, bool(self.rtl_tuner_agc))
                    except Exception:
                        pass

                elif drv == "hackrf":
                    # HackRF: antenna bias is exposed as 'bias_tx' in SoapyHackRF.
                    try:
                        soapy.write_setting(dev, "bias_tx", "true" if self.hackrf_bias_tx else "false")
                    except Exception:
                        pass
                    # Gain staging: use gain elements for predictable results.
                    try:
                        soapy.set_gain_element(dev, soapy.SOAPY_SDR_RX, chan, "LNA", float(self.hackrf_lna_db))
                        soapy.set_gain_element(dev, soapy.SOAPY_SDR_RX, chan, "VGA", float(self.hackrf_vga_db))
                        soapy.set_gain_element(dev, soapy.SOAPY_SDR_RX, chan, "AMP", 14.0 if self.hackrf_amp else 0.0)
                    except Exception:
                        # If gain elements aren't available, fall back to overall gain
                        try:
                            soapy.set_gain(dev, soapy.SOAPY_SDR_RX, chan, float(self.gain_db))
                        except Exception:
                            pass

                elif drv == "bladerf":
                    # bladeRF: bias-tee is per direction/channel.
                    try:
                        soapy.write_setting(dev, "biastee_rx", "true" if self.bladerf_biastee_rx else "false")
                    except Exception:
                        pass
                    try:
                        soapy.set_gain_mode(dev, soapy.SOAPY_SDR_RX, chan, bool(self.bladerf_agc))
                    except Exception:
                        pass

                soapy.set_sample_rate(dev, soapy.SOAPY_SDR_RX, chan, float(self.sample_rate_hz))
                try:
                    soapy.set_bandwidth(dev, soapy.SOAPY_SDR_RX, chan, float(self.sample_rate_hz))
                except Exception:
                    pass
                try:
                    if drv == "rtlsdr":
                        if not self.rtl_tuner_agc:
                            soapy.set_gain(dev, soapy.SOAPY_SDR_RX, chan, float(self.gain_db))
                    elif drv == "bladerf":
                        if not self.bladerf_agc:
                            soapy.set_gain(dev, soapy.SOAPY_SDR_RX, chan, float(self.gain_db))
                    elif drv not in ("hackrf",):
                        soapy.set_gain(dev, soapy.SOAPY_SDR_RX, chan, float(self.gain_db))
                except Exception:
                    pass

                soapy.set_frequency(dev, soapy.SOAPY_SDR_RX, chan, float(self._cf_hz))

                stream = soapy.setup_stream_cf32(dev, soapy.SOAPY_SDR_RX, chan)
                soapy.activate_stream(dev, stream)

                # Audio / demod settings
                audio_rate = 48_000
                # Demod selection
                mode = (self.demod_mode or "Auto").strip()
                if mode.lower() == "auto":
                    cf0 = int(self._cf_hz)
                    # Broadcast FM band => WFM
                    if 88_000_000 <= cf0 <= 108_000_000:
                        mode = "WFM"
                    # HF (rough heuristic) => SSB. Ham convention: LSB below ~10 MHz, USB above.
                    elif 100_000 <= cf0 < 30_000_000:
                        mode = "LSB" if cf0 < 10_000_000 else "USB"
                    else:
                        mode = "NFM"
                mode = mode.upper()
                wfm_decim_iq = 1
                wfm_fs = self.sample_rate_hz
                nfm_decim_iq = 1
                nfm_fs = self.sample_rate_hz
                # DSP helpers (kept lightweight; good enough for quick monitoring)
                def _one_pole_highpass(x: np.ndarray, state: dict, r: float = 0.995) -> np.ndarray:
                    # y[n] = x[n]-x[n-1] + r*y[n-1]
                    x = x.astype(np.float32, copy=False)
                    x_prev = state.get('x_prev', 0.0)
                    y_prev = state.get('y_prev', 0.0)
                    y = np.empty_like(x)
                    for i in range(len(x)):
                        xi = float(x[i])
                        yi = (xi - x_prev) + r * y_prev
                        y[i] = yi
                        x_prev = xi
                        y_prev = yi
                    state['x_prev'] = x_prev
                    state['y_prev'] = y_prev
                    return y

                def _deemphasis(x: np.ndarray, fs: float, state: dict, tau: float = 75e-6) -> np.ndarray:
                    # 1-pole low-pass deemphasis
                    a = 1.0 / (1.0 + (tau * fs))
                    y_prev = state.get('y_prev', 0.0)
                    y = np.empty_like(x, dtype=np.float32)
                    for i in range(len(x)):
                        y_prev = y_prev + a * (float(x[i]) - y_prev)
                        y[i] = y_prev
                    state['y_prev'] = y_prev
                    return y

                def _rms_agc(x: np.ndarray, target: float = 0.20) -> np.ndarray:
                    # Simple block AGC
                    rms = float(np.sqrt(np.mean(x * x)) + 1e-8)
                    g = target / rms
                    g = float(np.clip(g, 0.05, 10.0))
                    return x * g


                def _decimate_complex(xc: np.ndarray, decim: int) -> np.ndarray:
                    """Cheap low-pass + decimate by boxcar averaging in the complex domain."""
                    if decim <= 1:
                        return xc
                    m = (len(xc) // decim) * decim
                    if m <= 0:
                        return xc[:0]
                    return xc[:m].reshape(-1, decim).mean(axis=1).astype(np.complex64, copy=False)

                def _one_pole_lowpass(x: np.ndarray, state: dict, cutoff_hz: float, fs_hz: float) -> np.ndarray:
                    """One-pole low-pass for audio smoothing (helps hiss)."""
                    if cutoff_hz <= 0 or fs_hz <= 0:
                        return x
                    # alpha = 1 - exp(-2*pi*fc/fs)
                    a = 1.0 - float(np.exp(-2.0 * np.pi * (cutoff_hz / fs_hz)))
                    y_prev = float(state.get('y_prev', 0.0))
                    y = np.empty_like(x, dtype=np.float32)
                    for i in range(len(x)):
                        y_prev = y_prev + a * (float(x[i]) - y_prev)
                        y[i] = y_prev
                    state['y_prev'] = y_prev
                    return y

                hp_state = {}
                de_state = {}

                n = 8192
                buff = np.empty(n, np.complex64)

                last_cf = self._cf_hz
                prev = 1 + 0j

                
                if mode == "WFM":
                    # Pre-decimate complex stream to ~1.0 MHz to reduce wideband noise before FM discriminator
                    wfm_decim_iq = max(1, int(round(self.sample_rate_hz / 1_000_000))) if self.sample_rate_hz >= 1_000_000 else 1
                    wfm_fs = self.sample_rate_hz / wfm_decim_iq
                    # Two-stage decimation after discriminator: wfm_fs -> ~240 kHz -> audio_rate
                    decim1 = max(1, int(round(wfm_fs / 240_000))) if wfm_fs >= 240_000 else 1
                    inter_rate = wfm_fs / decim1
                    decim2 = max(1, int(round(inter_rate / audio_rate)))
                    eff_audio = inter_rate / decim2
                    self.status.emit(
                        f"Streaming WFM (Fs={self.sample_rate_hz/1e6:.2f}→{wfm_fs/1e6:.2f} MHz, audio≈{eff_audio:.0f} Hz)"
                    )
                elif mode in ("USB", "LSB"):
                    # SSB monitoring: same two-stage decimation for efficiency
                    decim1 = max(1, int(round(self.sample_rate_hz / 240_000))) if self.sample_rate_hz >= 240_000 else 1
                    inter_rate = self.sample_rate_hz / decim1
                    decim2 = max(1, int(round(inter_rate / audio_rate)))
                    eff_audio = inter_rate / decim2
                    self.status.emit(
                        f"Streaming {mode} (Fs={self.sample_rate_hz/1e6:.2f} MHz, audio≈{eff_audio:.0f} Hz)"
                    )
                else:
                    # Pre-decimate complex stream to ~240 kHz to reduce wideband noise before FM discriminator
                    nfm_decim_iq = max(1, int(round(self.sample_rate_hz / 240_000))) if self.sample_rate_hz >= 240_000 else 1
                    nfm_fs = self.sample_rate_hz / nfm_decim_iq
                    # NFM: decimate discriminator output to ~48 kHz
                    decim = max(1, int(round(nfm_fs / audio_rate)))
                    eff_audio = nfm_fs / decim
                    self.status.emit(
                        f"Streaming NFM (Fs={self.sample_rate_hz/1e6:.2f}→{nfm_fs/1e6:.2f} MHz, audio≈{eff_audio:.0f} Hz)"
                    )

                while not self._stop:
                    if self._cf_hz != last_cf:
                        soapy.set_frequency(dev, soapy.SOAPY_SDR_RX, chan, float(self._cf_hz))
                        last_cf = self._cf_hz
                        self.status.emit(f"Tuned: {last_cf} Hz")

                    ptr = buff.ctypes.data_as(ctypes.c_void_p)
                    buffs = (ctypes.c_void_p * 1)(ptr)
                    ret = soapy.read_stream(dev, stream, buffs, n, timeout_us=100000)
                    if ret <= 0:
                        time.sleep(0.005)
                        continue

                    x = buff[:ret].astype(np.complex64, copy=False)

                    if mode in ("USB", "LSB"):
                        # Very simple SSB monitor:
                        # - Decimate IQ to ~48 kHz
                        # - LSB is spectrally inverted relative to USB; conjugate fixes that
                        # - Take real part as audio
                        if decim1 > 1:
                            m1 = (len(x) // decim1) * decim1
                            if m1 <= 0:
                                continue
                            x1 = x[:m1].reshape(-1, decim1).mean(axis=1)
                        else:
                            x1 = x

                        if decim2 > 1:
                            m2 = (len(x1) // decim2) * decim2
                            if m2 <= 0:
                                continue
                            x2 = x1[:m2].reshape(-1, decim2).mean(axis=1)
                        else:
                            x2 = x1

                        if mode == "LSB":
                            x2 = np.conj(x2)

                        audio = np.real(x2).astype(np.float32, copy=False)
                        # DC block + light AGC
                        audio = _one_pole_highpass(audio, hp_state, r=0.995)
                        audio = _rms_agc(audio, target=0.22)
                        audio = np.clip(audio, -1.0, 1.0)

                    else:
                        # FM discriminator (quadrature) - run at reduced complex rate to cut wideband hiss
                        if mode == "WFM":
                            x_c = _decimate_complex(x, wfm_decim_iq)
                            fs_c = wfm_fs
                        else:
                            x_c = _decimate_complex(x, nfm_decim_iq)
                            fs_c = nfm_fs

                        if len(x_c) == 0:
                            continue

                        x0 = np.empty(len(x_c) + 1, np.complex64)
                        x0[0] = prev
                        x0[1:] = x_c
                        prod = x0[1:] * np.conj(x0[:-1])
                        fm = np.angle(prod).astype(np.float32)
                        prev = complex(x_c[-1])

                        if mode == "WFM":
                            # Stage 1: decimate to ~240 kHz
                            m1 = (len(fm) // decim1) * decim1
                            if m1 <= 0:
                                continue
                            fm1 = fm[:m1].reshape(-1, decim1).mean(axis=1)

                            # Stage 2: decimate to ~48 kHz
                            m2 = (len(fm1) // decim2) * decim2
                            if m2 <= 0:
                                continue
                            audio = fm1[:m2].reshape(-1, decim2).mean(axis=1)

                            # WFM: deemphasis + AGC
                            audio = _one_pole_highpass(audio, hp_state, r=0.995)
                            audio = _deemphasis(audio, fs=float(eff_audio), state=de_state, tau=75e-6)
                            audio = _one_pole_lowpass(audio, de_state, cutoff_hz=15000.0, fs_hz=eff_audio)
                            audio = _rms_agc(audio, target=0.20)
                            audio = np.clip(audio, -1.0, 1.0)
                        else:
                            # NFM: decimate directly to ~48 kHz
                            m0 = (len(fm) // decim) * decim
                            if m0 <= 0:
                                continue
                            audio = fm[:m0].reshape(-1, decim).mean(axis=1)

                            # NFM: light DC block + AGC
                            audio = _one_pole_highpass(audio, hp_state, r=0.995)
                            audio = _rms_agc(audio, target=0.22)
                            audio = _one_pole_lowpass(audio, de_state, cutoff_hz=3000.0, fs_hz=eff_audio)
                            audio = np.clip(audio, -1.0, 1.0)

                    pcm_m = (audio * 32767.0).astype(np.int16)
                    # Duplicate mono to stereo (L/R) for broad device compatibility
                    pcm = np.repeat(pcm_m[:, None], 2, axis=1).reshape(-1).tobytes()
                    self.audio_bytes.emit(pcm)

            finally:
                try:
                    if stream is not None:
                        try:
                            soapy.deactivate_stream(dev, stream)
                        except Exception:
                            pass
                        try:
                            soapy.close_stream(dev, stream)
                        except Exception:
                            pass
                finally:
                    try:
                        soapy.unmake(dev)
                    except Exception:
                        pass

        except Exception as e:
            msg = str(e)
            low = msg.lower()
            if any(k in low for k in ("busy", "resource busy", "in use", "occupied", "claim")):
                self.error.emit(
                    "That SDR appears to be IN USE.\n\n"
                    "It’s probably being used by Watchdog scanning (or another SDR app).\n"
                    "Stop scanning / close the other app to free the device and try again.\n\n"
                    f"Details: {msg}"
                )
            else:
                self.error.emit(f"CF tuner error:\n\n{msg}")


# ---------------------------
# CF Tuner popup window
# ---------------------------

class CenterFrequencyTunerWindow(QtWidgets.QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Center Frequency Tuner")
        self.setModal(False)
        self.resize(560, 240)

        self._thread: Optional[_RxAudioThread] = None

        # UI
        self.device_combo = QtWidgets.QComboBox()
        self.refresh_btn = QtWidgets.QPushButton("Refresh")

        self.args_edit = QtWidgets.QLineEdit()
        self.args_edit.setPlaceholderText("driver=rtlsdr  OR  driver=bladerf,serial=...")

        self.cf_spin = GroupedHzSpinBox()
        # Restore last-used CF (Hz). Do not remember device selection.
        s = QtCore.QSettings()
        try:
            last_cf = s.value("watchdog/cf_tuner_last_hz", 162_550_000)
            if isinstance(last_cf, str):
                last_cf = int(last_cf.strip().replace(".", "").replace(" ", "") or "162550000")
            self.cf_spin.setValue(int(last_cf))
        except Exception:
            self.cf_spin.setValue(162_550_000)

        self.demod_combo = QtWidgets.QComboBox()
        self.demod_combo.addItems(["Auto", "NFM", "WFM", "USB", "LSB"])
        self.demod_combo.setCurrentText("Auto")
        self.demod_combo.setToolTip("Audio demodulation mode. Auto selects WFM for 88–108 MHz; USB/LSB for HF.")
        self.demod_combo.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToContents)
        self.demod_combo.setMinimumWidth(80)

        self.sr_combo = QtWidgets.QComboBox()
        # Will be repopulated with sensible presets based on selected SDR.
        # Make sure the dropdown is wide enough in dark mode / small layouts.
        self.sr_combo.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToContentsOnFirstShow)
        self.sr_combo.setMinimumContentsLength(10)
        self.sr_combo.setMinimumWidth(140)
        try:
            self.sr_combo.view().setMinimumWidth(180)
        except Exception:
            pass
        # Generic defaults (overridden per-device)
        for hz in (900_000, 1_000_000, 1_920_000, 2_048_000, 2_400_000, 3_200_000, 4_800_000):
            self.sr_combo.addItem(f"{hz/1e6:.3f} MSps", userData=int(hz))
        self.sr_combo.setCurrentText("2.400 MSps")

        self.gain_spin = QtWidgets.QDoubleSpinBox()
        self.gain_spin.setDecimals(0)
        self.gain_spin.setRange(0.0, 70.0)
        self.gain_spin.setSingleStep(1.0)
        self.gain_spin.setValue(30.0)

        self.start_btn = QtWidgets.QPushButton("Start")
        self.stop_btn = QtWidgets.QPushButton("Stop")
        self.stop_btn.setEnabled(False)

        self.mute_chk = QtWidgets.QCheckBox("Mute")
        self.vol_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.vol_slider.setRange(0, 100)
        self.vol_slider.setValue(70)

        self.status_lbl = QtWidgets.QLabel("Idle.")

        # Layout
        form = QtWidgets.QFormLayout()
        dev_row = QtWidgets.QHBoxLayout()
        dev_row.addWidget(self.device_combo, 1)
        dev_row.addWidget(self.refresh_btn)
        form.addRow("Device:", dev_row)
        form.addRow("Args:", self.args_edit)
        cf_row = QtWidgets.QHBoxLayout()
        cf_row.addWidget(self.cf_spin, 1)
        cf_row.addSpacing(8)
        cf_row.addWidget(QtWidgets.QLabel("Demod:"))
        cf_row.addWidget(self.demod_combo)
        form.addRow("Center freq:", cf_row)

        tune_row = QtWidgets.QHBoxLayout()
        tune_row.addWidget(QtWidgets.QLabel("Sample rate:"))
        tune_row.addWidget(self.sr_combo)
        tune_row.addSpacing(12)
        tune_row.addWidget(QtWidgets.QLabel("Gain:"))
        tune_row.addWidget(self.gain_spin)
        tune_row.addStretch(1)

        # RTL-SDR-specific options (only shown when driver=rtlsdr is selected)
        self.rtl_group = QtWidgets.QGroupBox("RTL-SDR options")
        rtl_grid = QtWidgets.QGridLayout(self.rtl_group)

        self.rtl_offset_tune = QtWidgets.QCheckBox("Offset tune")
        self.rtl_offset_tune.setChecked(True)

        self.rtl_digital_agc = QtWidgets.QCheckBox("Digital AGC")
        self.rtl_digital_agc.setChecked(False)

        self.rtl_tuner_agc = QtWidgets.QCheckBox("Tuner AGC")
        self.rtl_tuner_agc.setChecked(False)
        self.rtl_tuner_agc.setToolTip("When enabled, the tuner controls gain automatically and the Gain field is ignored.")

        self.rtl_iq_swap = QtWidgets.QCheckBox("I/Q swap")
        self.rtl_iq_swap.setChecked(False)

        self.rtl_biastee = QtWidgets.QCheckBox("Bias-Tee")
        self.rtl_biastee.setChecked(False)

        self.rtl_direct_samp = QtWidgets.QComboBox()
        self.rtl_direct_samp.addItem("Off", "0")
        self.rtl_direct_samp.addItem("I", "1")
        self.rtl_direct_samp.addItem("Q", "2")

        # Layout (2 rows x 3 cols)
        rtl_grid.addWidget(self.rtl_offset_tune, 0, 0)
        rtl_grid.addWidget(self.rtl_digital_agc, 0, 1)
        rtl_grid.addWidget(self.rtl_tuner_agc, 0, 2)
        rtl_grid.addWidget(self.rtl_iq_swap, 1, 0)
        rtl_grid.addWidget(self.rtl_biastee, 1, 1)
        rtl_grid.addWidget(QtWidgets.QLabel("Direct sampling:"), 1, 2)
        rtl_grid.addWidget(self.rtl_direct_samp, 1, 3)

        self.rtl_group.setVisible(False)

        # HackRF-specific options (shown only when driver=hackrf)
        self.hackrf_group = QtWidgets.QGroupBox("HackRF options")
        hack_grid = QtWidgets.QGridLayout(self.hackrf_group)

        self.hackrf_bias_tx = QtWidgets.QCheckBox("Antenna bias (bias_tx)")
        self.hackrf_bias_tx.setToolTip("SoapyHackRF setting 'bias_tx' (Antenna port power control).")
        self.hackrf_bias_tx.setChecked(False)

        self.hackrf_amp = QtWidgets.QCheckBox("RF amp (AMP)")
        self.hackrf_amp.setToolTip("Enables the HackRF RF amplifier (AMP gain element: 0 or 14 dB).")
        self.hackrf_amp.setChecked(True)

        self.hackrf_lna = QtWidgets.QSpinBox()
        self.hackrf_lna.setRange(0, 40)
        self.hackrf_lna.setSingleStep(8)
        self.hackrf_lna.setValue(32)
        self.hackrf_lna.setToolTip("HackRF LNA gain element (0–40 dB in 8 dB steps).")

        self.hackrf_vga = QtWidgets.QSpinBox()
        self.hackrf_vga.setRange(0, 62)
        self.hackrf_vga.setSingleStep(2)
        self.hackrf_vga.setValue(20)
        self.hackrf_vga.setToolTip("HackRF VGA gain element (0–62 dB in 2 dB steps).")

        hack_grid.addWidget(self.hackrf_bias_tx, 0, 0, 1, 2)
        hack_grid.addWidget(self.hackrf_amp, 0, 2, 1, 2)
        hack_grid.addWidget(QtWidgets.QLabel("LNA (dB):"), 1, 0)
        hack_grid.addWidget(self.hackrf_lna, 1, 1)
        hack_grid.addWidget(QtWidgets.QLabel("VGA (dB):"), 1, 2)
        hack_grid.addWidget(self.hackrf_vga, 1, 3)

        self.hackrf_group.setVisible(False)


        # bladeRF-specific options (shown only when driver=bladerf)
        self.bladerf_group = QtWidgets.QGroupBox("bladeRF options")
        blade_grid = QtWidgets.QGridLayout(self.bladerf_group)

        self.bladerf_agc = QtWidgets.QCheckBox("RX AGC")
        self.bladerf_agc.setToolTip("Enable bladeRF RX AGC (Soapy gain mode). When enabled, Gain is ignored.")
        self.bladerf_agc.setChecked(False)

        self.bladerf_biastee_rx = QtWidgets.QCheckBox("Bias-Tee RX (biastee_rx)")
        self.bladerf_biastee_rx.setToolTip("Enable bladeRF bias tee on RX channel 0 (Soapy setting 'biastee_rx').")
        self.bladerf_biastee_rx.setChecked(False)

        blade_grid.addWidget(self.bladerf_agc, 0, 0)
        blade_grid.addWidget(self.bladerf_biastee_rx, 0, 1)

        self.bladerf_group.setVisible(False)


        btn_row = QtWidgets.QHBoxLayout()
        btn_row.addWidget(self.start_btn)
        btn_row.addWidget(self.stop_btn)
        btn_row.addStretch(1)
        btn_row.addWidget(self.mute_chk)

        vol_row = QtWidgets.QHBoxLayout()
        vol_row.addWidget(QtWidgets.QLabel("Volume:"))
        vol_row.addWidget(self.vol_slider, 1)

        root = QtWidgets.QVBoxLayout(self)
        root.addLayout(form)
        root.addLayout(tune_row)
        root.addLayout(btn_row)
        root.addLayout(vol_row)
        root.addWidget(self.rtl_group)
        root.addWidget(self.hackrf_group)
        root.addWidget(self.bladerf_group)
        root.addWidget(self.status_lbl)

        # Audio output (16-bit mono @ 48k)
        fmt = QAudioFormat()
        fmt.setChannelCount(2)
        fmt.setSampleRate(48_000)
        fmt.setSampleSize(16)
        fmt.setCodec("audio/pcm")
        fmt.setByteOrder(QAudioFormat.LittleEndian)
        fmt.setSampleType(QAudioFormat.SignedInt)

        self.audio_out = QAudioOutput(fmt, self)
        self.audio_io = None
        self._apply_volume()
        self._fix_checkbox_indicator_style()

        # Signals
        self.refresh_btn.clicked.connect(self.refresh_devices)
        self.device_combo.currentIndexChanged.connect(self._on_device_selected)
        self.args_edit.textChanged.connect(self._on_args_changed)
        # Keep Gain enable/disable in sync with AGC toggles
        try:
            self.rtl_tuner_agc.toggled.connect(self._update_device_specific_ui)
        except Exception:
            pass
        try:
            self.bladerf_agc.toggled.connect(self._update_device_specific_ui)
        except Exception:
            pass

        self.start_btn.clicked.connect(self.start_listening)
        self.stop_btn.clicked.connect(self.stop_listening)

        self.vol_slider.valueChanged.connect(self._apply_volume)
        self.mute_chk.toggled.connect(self._apply_volume)
        self.cf_spin.valueChanged.connect(self._on_cf_changed)

        self._busy_hackrf_serials = set()
        self._busy_soapy_args = set()

        self.refresh_devices()

    def _fix_checkbox_indicator_style(self):
        # Some dark themes make unchecked checkboxes nearly invisible.
        # Give the indicator an explicit border/background so it remains readable.
        try:
            self.setStyleSheet(self.styleSheet() + "QCheckBox::indicator { width: 14px; height: 14px; border: 1px solid #808080; border-radius: 2px; background: rgba(0,0,0,0.25);} QCheckBox::indicator:checked { background: rgba(255,255,255,0.25);} QCheckBox::indicator:disabled { border: 1px solid #505050; background: rgba(0,0,0,0.15);} ")
        except Exception:
            pass

    def set_frequency_hz(self, hz: int):
        """Set the CF field (Hz) without auto-start."""
        try:
            self.cf_spin.setValue(int(hz))
        except Exception:
            pass

    def set_busy_devices(self, busy_hackrf_serials=None, busy_soapy_args=None):
        """Provide a snapshot of devices currently used by Watchdog scanning."""
        self._busy_hackrf_serials = set(busy_hackrf_serials or [])
        self._busy_soapy_args = set(busy_soapy_args or [])

    def auto_select_free_device(self):
        """Pick the first device that is not currently in use by scanning."""
        for i in range(self.device_combo.count()):
            args = str(self.device_combo.itemData(i) or "").strip()
            if not args:
                continue

            if "driver=hackrf" in args and "serial=" in args:
                serial = args.split("serial=", 1)[1].split(",", 1)[0].strip()
                if serial and serial in getattr(self, "_busy_hackrf_serials", set()):
                    continue
                self.device_combo.setCurrentIndex(i)
                return

            if args in getattr(self, "_busy_soapy_args", set()):
                continue

            self.device_combo.setCurrentIndex(i)
            return

    def refresh_devices(self):
        self.device_combo.blockSignals(True)
        self.device_combo.clear()

        soapy = list_soapy_devices_allow_all()
        if soapy:
            for dev in soapy:
                label = f"SoapySDR – {dev.get('label')}"
                args = str(dev.get("args_str") or "")
                self.device_combo.addItem(label, userData=args)
        else:
            self.device_combo.addItem("No SoapySDRUtil devices found (or SoapySDRUtil missing).", userData="")

        for serial in list_hackrf_serials():
            args = f"driver=hackrf,serial={serial}"
            self.device_combo.addItem(f"HackRF (via Soapy) – {serial}", userData=args)

        self.device_combo.blockSignals(False)
        self.auto_select_free_device()
        self._on_device_selected()

    def _on_device_selected(self, _idx: int = 0):
        args = self.device_combo.currentData()
        if isinstance(args, str) and args:
            self.args_edit.blockSignals(True)
            try:
                self.args_edit.setText(args)
            finally:
                self.args_edit.blockSignals(False)
        self._update_device_specific_ui()

    def _on_args_changed(self, _text: str):
        self._update_device_specific_ui()

    
    @staticmethod
    def _is_driver(args: str, driver: str) -> bool:
        a = (args or "").strip().lower().replace(" ", "")
        return f"driver={driver.lower()}" in a

    def _set_sample_rate_presets(self, presets_hz: List[int], preferred_hz: int):
        cur = int(self.sr_combo.currentData() or 0)
        self.sr_combo.blockSignals(True)
        try:
            self.sr_combo.clear()
            for hz in presets_hz:
                self.sr_combo.addItem(f"{hz/1e6:.3f} MSps", userData=int(hz))
            pick = preferred_hz
            if pick not in presets_hz:
                pick = min(presets_hz, key=lambda x: abs(x - (cur or preferred_hz)))
            idx = self.sr_combo.findData(int(pick))
            if idx >= 0:
                self.sr_combo.setCurrentIndex(idx)
        finally:
            self.sr_combo.blockSignals(False)

    def _update_device_specific_ui(self):
        args = str(self.args_edit.text() or "")
        is_rtl = self._is_driver(args, "rtlsdr")
        is_hackrf = self._is_driver(args, "hackrf")
        is_bladerf = self._is_driver(args, "bladerf")

        self.rtl_group.setVisible(bool(is_rtl))
        self.hackrf_group.setVisible(bool(is_hackrf))
        self.bladerf_group.setVisible(bool(is_bladerf))

        # Generic Gain field is ignored for some devices/modes.
        enable_gain = True
        if is_hackrf:
            enable_gain = False  # use HackRF LNA/VGA/AMP controls
        if is_rtl and getattr(self, "rtl_tuner_agc", None) is not None and self.rtl_tuner_agc.isChecked():
            enable_gain = False
        if is_bladerf and getattr(self, "bladerf_agc", None) is not None and self.bladerf_agc.isChecked():
            enable_gain = False
        try:
            self.gain_spin.setEnabled(bool(enable_gain))
        except Exception:
            pass


        if not hasattr(self, "_sr_mode"):
            self._sr_mode = None
        mode = "rtl" if is_rtl else ("hackrf" if is_hackrf else ("bladerf" if is_bladerf else "generic"))
        if mode != getattr(self, "_sr_mode", None):
            self._sr_mode = mode
            if mode == "rtl":
                presets = [900_000, 1_024_000, 1_200_000, 1_536_000, 1_800_000, 1_920_000, 2_048_000, 2_400_000, 2_800_000, 3_200_000]
                self._set_sample_rate_presets(presets, 2_400_000)
                self.gain_spin.setRange(0.0, 49.6)
            elif mode == "hackrf":
                presets = [2_000_000, 4_000_000, 8_000_000, 10_000_000, 20_000_000]
                self._set_sample_rate_presets(presets, 2_000_000)
                self.gain_spin.setRange(0.0, 70.0)
            elif mode == "bladerf":
                presets = [2_000_000, 4_000_000, 8_000_000, 10_000_000, 15_360_000, 20_000_000]
                self._set_sample_rate_presets(presets, 2_000_000)
                self.gain_spin.setRange(-15.0, 60.0)
            else:
                presets = [900_000, 1_000_000, 1_920_000, 2_048_000, 2_400_000, 3_200_000, 4_800_000, 10_000_000, 20_000_000]
                self._set_sample_rate_presets(presets, 2_400_000)
                self.gain_spin.setRange(0.0, 70.0)

    
    def _gather_rtl_settings(self) -> Dict[str, str]:
        """Collect RTL-SDR settings to apply via Soapy writeSetting()."""
        settings: Dict[str, str] = {}
        settings["offset_tune"] = "true" if self.rtl_offset_tune.isChecked() else "false"
        settings["digital_agc"] = "true" if self.rtl_digital_agc.isChecked() else "false"
        settings["iq_swap"] = "true" if self.rtl_iq_swap.isChecked() else "false"
        settings["biastee"] = "true" if self.rtl_biastee.isChecked() else "false"
        settings["direct_samp"] = str(self.rtl_direct_samp.currentData() or "0")
        return settings

    def _gather_hackrf_params(self):
        """HackRF params: (lna_db, vga_db, amp_enabled, bias_tx_enabled)"""
        return (
            float(self.hackrf_lna.value()),
            float(self.hackrf_vga.value()),
            bool(self.hackrf_amp.isChecked()),
            bool(self.hackrf_bias_tx.isChecked()),
        )

    def _gather_bladerf_params(self):
        """bladeRF params: (agc_enabled, biastee_rx_enabled)"""
        return (
            bool(self.bladerf_agc.isChecked()),
            bool(self.bladerf_biastee_rx.isChecked()),
        )


    def _apply_volume(self):
        v = 0.0 if self.mute_chk.isChecked() else (self.vol_slider.value() / 100.0)
        self.audio_out.setVolume(float(v))

    def _on_cf_changed(self, _):
        if self._thread is not None:
            self._thread.set_center_freq(int(self.cf_spin.value()))


    def start_listening(self):
        if self._thread is not None:
            return

        args = str(self.args_edit.text() or "").strip()

        # Failsafe: if selected device is currently used by Watchdog scanning, warn and don't start.
        if args:
            if ("driver=hackrf" in args and "serial=" in args):
                serial = args.split("serial=", 1)[1].split(",", 1)[0].strip()
                if serial and serial in getattr(self, "_busy_hackrf_serials", set()):
                    QtWidgets.QMessageBox.warning(
                        self,
                        "Device In Use",
                        "This HackRF (" + str(serial) + ") is currently in use by Watchdog scanning.\n\n"
                        "Stop scanning or choose a different device first."
                    )
                    return
            if args in getattr(self, "_busy_soapy_args", set()):
                QtWidgets.QMessageBox.warning(
                    self,
                    "Device In Use",
                    "This SDR is currently in use by Watchdog scanning.\n\n"
                    "Stop scanning or choose a different device first."
                )
                return
        if not args:
            QtWidgets.QMessageBox.warning(self, "CF Tuner", "No device args selected. Try Refresh.")
            return

        # Ensure audio output is started
        if self.audio_io is None:
            self.audio_io = self.audio_out.start()

        sr = int(self.sr_combo.currentData() or 2_400_000)

        # Determine selected driver from args (used for both UI and backend behavior)
        drv = "generic"
        if self._is_driver(args, "rtlsdr"):
            drv = "rtlsdr"
        elif self._is_driver(args, "hackrf"):
            drv = "hackrf"
        elif self._is_driver(args, "bladerf"):
            drv = "bladerf"

        # Device-specific settings
        rtl_settings: Dict[str, str] = {}
        rtl_tuner_agc = False

        hackrf_lna, hackrf_vga, hackrf_amp, hackrf_bias = (32.0, 20.0, True, False)
        bladerf_agc, bladerf_biastee_rx = (False, False)

        if drv == "rtlsdr":
            rtl_settings = self._gather_rtl_settings()
            rtl_tuner_agc = bool(self.rtl_tuner_agc.isChecked())
        elif drv == "hackrf":
            hackrf_lna, hackrf_vga, hackrf_amp, hackrf_bias = self._gather_hackrf_params()
        elif drv == "bladerf":
            bladerf_agc, bladerf_biastee_rx = self._gather_bladerf_params()

        self._thread = _RxAudioThread(
            soapy_args=args,
            device_driver=drv,
            center_hz=int(self.cf_spin.value()),
            sample_rate_hz=sr,
            gain_db=float(self.gain_spin.value()),
            demod_mode=str(self.demod_combo.currentText()),
            rtl_settings=rtl_settings,
            rtl_tuner_agc=rtl_tuner_agc,
            hackrf_lna_db=hackrf_lna,
            hackrf_vga_db=hackrf_vga,
            hackrf_amp=hackrf_amp,
            hackrf_bias_tx=hackrf_bias,
            bladerf_agc=bladerf_agc,
            bladerf_biastee_rx=bladerf_biastee_rx,
            parent=self,
        )

        # Wire thread signals
        self._thread.audio_bytes.connect(self._write_audio, QtCore.Qt.QueuedConnection)
        self._thread.error.connect(self._on_error, QtCore.Qt.QueuedConnection)
        self._thread.status.connect(self.status_lbl.setText, QtCore.Qt.QueuedConnection)
        self._thread.finished.connect(self._on_finished)

        # Lock UI while running
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.device_combo.setEnabled(False)
        self.refresh_btn.setEnabled(False)
        self.args_edit.setEnabled(False)
        self.sr_combo.setEnabled(False)
        self.gain_spin.setEnabled(False)

        self._thread.start()

    @QtCore.pyqtSlot(bytes)
    def _write_audio(self, pcm: bytes):
        if self.audio_io is None:
            return
        try:
            self.audio_io.write(pcm)
        except Exception:
            pass

    @QtCore.pyqtSlot(str)
    def _on_error(self, msg: str):
        self.stop_listening()
        QtWidgets.QMessageBox.critical(self, "CF Tuner", msg)

    def stop_listening(self):
        if self._thread is not None:
            try:
                self._thread.stop()
            except Exception:
                pass

        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.device_combo.setEnabled(True)
        self.refresh_btn.setEnabled(True)
        self.args_edit.setEnabled(True)
        self.sr_combo.setEnabled(True)
        self.gain_spin.setEnabled(True)

    def _on_finished(self):
        self._thread = None
        self.status_lbl.setText("Stopped.")
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.device_combo.setEnabled(True)
        self.refresh_btn.setEnabled(True)
        self.args_edit.setEnabled(True)
        self.sr_combo.setEnabled(True)
        self.gain_spin.setEnabled(True)

    def closeEvent(self, event):
        try:
            self.stop_listening()
        except Exception:
            pass
        super().closeEvent(event)

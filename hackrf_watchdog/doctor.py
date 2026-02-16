from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import json
import os
import shutil
import subprocess
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional, Tuple

from .device_discovery import (
    find_soapy_util,
    list_hackrf_devices,
    parse_soapy_find_output,
)


@dataclass
class CheckResult:
    ok: bool
    summary: str
    details: List[str]


def action_hints_from_output(text: str) -> List[str]:
    t = (text or "").lower()
    hints: List[str] = []

    if "hackrf_init() failed" in t:
        hints.append("HackRF runtime init failed. Check USB cable/power, udev rules, and if another app is holding the device.")
    if "libusb" in t and "hotplug" in t:
        hints.append("Soapy/libusb hotplug failed. Verify USB permissions/udev and that libusb can access SDR devices.")
    if "permission denied" in t or "access denied" in t:
        hints.append("Permission issue detected. On Linux, confirm user is in required groups and udev rules are installed.")
    if "no matches" in t or "no devices" in t:
        hints.append("No SDR devices discovered. Confirm hardware is connected and recognized by the OS.")

    return hints


def _run_text(cmd: List[str], timeout_s: float = 8.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_s,
    )


def _load_soapy_runtime() -> Tuple[bool, str]:
    try:
        if os.name == "nt":
            roots: List[str] = []
            env_root = os.environ.get("POTHOS")
            if env_root:
                roots.append(env_root)
            roots.extend([r"C:\Program Files\PothosSDR", r"C:\Program Files (x86)\PothosSDR"])
            for root in roots:
                dll_path = os.path.join(root, "bin", "SoapySDR.dll")
                if os.path.isfile(dll_path):
                    ctypes.WinDLL(dll_path)
                    return True, f"Loaded SoapySDR runtime: {dll_path}"

            ctypes.WinDLL("SoapySDR.dll")
            return True, "Loaded SoapySDR runtime from PATH (SoapySDR.dll)"

        name = ctypes.util.find_library("SoapySDR")
        candidates = [name, "libSoapySDR.so", "libSoapySDR.so.0.8", "libSoapySDR.so.0.8.1"]
        for cand in candidates:
            if not cand:
                continue
            try:
                ctypes.CDLL(cand)
                return True, f"Loaded SoapySDR runtime: {cand}"
            except OSError:
                continue

        return False, "Could not locate/load SoapySDR runtime shared library"
    except Exception as e:
        return False, f"SoapySDR runtime load failed: {e}"


def _driver_counts(soapy_devices: List[Dict[str, Any]]) -> Dict[str, int]:
    rtl = 0
    bladerf = 0

    for dev in soapy_devices:
        dlow = str(dev.get("driver_lower") or dev.get("driver") or "").lower()
        if "rtl" in dlow:
            rtl += 1
        if "bladerf" in dlow:
            bladerf += 1

    return {"rtlsdr": rtl, "bladerf": bladerf}


def evaluate_required_backends(
    *,
    hackrf_ok: bool,
    soapy_runtime_ok: bool,
    rtlsdr_count: int,
    bladerf_count: int,
) -> Tuple[bool, List[str]]:
    failures: List[str] = []
    if not hackrf_ok:
        failures.append("HackRF backend is not healthy")
    if not soapy_runtime_ok:
        failures.append("Soapy runtime is not healthy")
    if int(rtlsdr_count) < 1:
        failures.append("No RTL-SDR device detected via Soapy")
    if int(bladerf_count) < 1:
        failures.append("No bladeRF device detected via Soapy")

    return (len(failures) == 0, failures)


def check_hackrf_backend(require_device: bool = True) -> Dict[str, Any]:
    details: List[str] = []
    info_exe = shutil.which("hackrf_info")
    sweep_exe = shutil.which("hackrf_sweep")

    if info_exe:
        details.append(f"hackrf_info: {info_exe}")
    else:
        details.append("hackrf_info: not found on PATH")

    if sweep_exe:
        details.append(f"hackrf_sweep: {sweep_exe}")
    else:
        details.append("hackrf_sweep: not found on PATH")

    output = ""
    rc: Optional[int] = None
    devices: List[Dict[str, str]] = []
    runtime_ok = False

    if info_exe:
        try:
            res = _run_text([info_exe], timeout_s=6.0)
            rc = int(res.returncode)
            output = res.stdout or ""
            runtime_ok = (rc == 0)
            devices = list_hackrf_devices()
            details.append(f"hackrf_info exit code: {rc}")
            details.append(f"HackRF devices detected: {len(devices)}")
        except Exception as e:
            details.append(f"hackrf_info execution error: {e}")

    hints = action_hints_from_output(output)
    details.extend(hints)

    exe_ok = bool(info_exe and sweep_exe)
    device_ok = (len(devices) > 0) if require_device else True
    ok = bool(exe_ok and runtime_ok and device_ok)

    summary = "HackRF backend healthy" if ok else "HackRF backend not ready"
    return {
        "ok": ok,
        "summary": summary,
        "details": details,
        "runtime_ok": runtime_ok,
        "executables_ok": exe_ok,
        "devices": devices,
        "device_count": len(devices),
        "output_tail": (output or "")[-800:],
    }


def check_soapy_backend(
    *,
    require_rtlsdr: bool,
    require_bladerf: bool,
    require_util: bool = True,
) -> Dict[str, Any]:
    details: List[str] = []
    util = find_soapy_util()

    if util:
        details.append(f"SoapySDRUtil: {util}")
    else:
        details.append("SoapySDRUtil: not found on PATH")

    runtime_ok, runtime_msg = _load_soapy_runtime()
    details.append(runtime_msg)

    soapy_devices: List[Dict[str, Any]] = []
    soapy_output = ""
    soapy_rc: Optional[int] = None

    if util:
        try:
            res = _run_text([util, "--find"], timeout_s=10.0)
            soapy_rc = int(res.returncode)
            soapy_output = res.stdout or ""
            soapy_devices = parse_soapy_find_output(soapy_output)
            details.append(f"SoapySDRUtil --find exit code: {soapy_rc}")
            details.append(f"Soapy devices parsed (non-audio/non-hackrf): {len(soapy_devices)}")
        except Exception as e:
            details.append(f"SoapySDRUtil --find failed: {e}")

    hints = action_hints_from_output(soapy_output)
    details.extend(hints)

    counts = _driver_counts(soapy_devices)
    details.append(f"RTL-SDR devices via Soapy: {counts['rtlsdr']}")
    details.append(f"bladeRF devices via Soapy: {counts['bladerf']}")

    util_ok = bool(util or not require_util)
    rtl_ok = (counts["rtlsdr"] >= 1) if require_rtlsdr else True
    blade_ok = (counts["bladerf"] >= 1) if require_bladerf else True
    ok = bool(runtime_ok and util_ok and rtl_ok and blade_ok)

    summary = "Soapy backend healthy" if ok else "Soapy backend not ready"
    return {
        "ok": ok,
        "summary": summary,
        "details": details,
        "runtime_ok": runtime_ok,
        "util_ok": util_ok,
        "util_path": util,
        "soapy_find_exit_code": soapy_rc,
        "devices": soapy_devices,
        "counts": counts,
        "output_tail": (soapy_output or "")[-800:],
    }


def build_doctor_report() -> Dict[str, Any]:
    hackrf = check_hackrf_backend(require_device=True)
    soapy = check_soapy_backend(require_rtlsdr=True, require_bladerf=True)

    overall_ok, failures = evaluate_required_backends(
        hackrf_ok=bool(hackrf.get("ok")),
        soapy_runtime_ok=bool(soapy.get("runtime_ok")),
        rtlsdr_count=int(soapy.get("counts", {}).get("rtlsdr", 0)),
        bladerf_count=int(soapy.get("counts", {}).get("bladerf", 0)),
    )

    # Keep the strict check aligned with the required matrix.
    overall_ok = bool(overall_ok and bool(soapy.get("util_ok")))
    if not soapy.get("util_ok"):
        failures.append("SoapySDRUtil is not available")

    return {
        "overall_ok": overall_ok,
        "failures": failures,
        "hackrf": hackrf,
        "soapy": soapy,
    }


def format_human_report(report: Dict[str, Any]) -> str:
    lines: List[str] = []
    lines.append("Watchdog Backend Doctor")
    lines.append("=" * 24)
    lines.append(f"Overall: {'PASS' if report.get('overall_ok') else 'FAIL'}")

    lines.append("")
    lines.append("[HackRF]")
    lines.append(f"Status: {'PASS' if report.get('hackrf', {}).get('ok') else 'FAIL'}")
    for d in report.get("hackrf", {}).get("details", []):
        lines.append(f"- {d}")

    lines.append("")
    lines.append("[Soapy]")
    lines.append(f"Status: {'PASS' if report.get('soapy', {}).get('ok') else 'FAIL'}")
    for d in report.get("soapy", {}).get("details", []):
        lines.append(f"- {d}")

    failures = report.get("failures", []) or []
    if failures:
        lines.append("")
        lines.append("Required failures:")
        for f in failures:
            lines.append(f"- {f}")

    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Watchdog backend readiness checks (HackRF + Soapy).")
    parser.add_argument("--json", action="store_true", help="Output JSON report")
    args = parser.parse_args(argv)

    report = build_doctor_report()
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(format_human_report(report))

    return 0 if report.get("overall_ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

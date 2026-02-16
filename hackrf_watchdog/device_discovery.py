from __future__ import annotations

import os
import re
import shutil
import subprocess
from typing import Any, Dict, List, Optional


def parse_hackrf_info_output(text: str) -> List[Dict[str, str]]:
    """Parse `hackrf_info` output into [{'index': str, 'serial': str}, ...]."""
    devices: List[Dict[str, str]] = []
    index = -1

    for line in (text or "").splitlines():
        raw = line.strip()
        low = raw.lower()

        if low.startswith("found hackrf"):
            index += 1

        if ":" not in raw:
            continue
        if "serial" not in low:
            continue

        _, val = raw.split(":", 1)
        serial = "".join(ch for ch in val.strip() if ch in "0123456789abcdefABCDEF")
        if not serial:
            continue

        if index < 0:
            index = len(devices)

        devices.append({"index": str(index), "serial": serial})

    return devices


def parse_soapy_args(args_str: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for part in (args_str or "").split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, val = part.split("=", 1)
        out[key.strip().lower()] = val.strip()
    return out


def parse_soapy_find_output(text: str) -> List[Dict[str, Any]]:
    """Parse `SoapySDRUtil --find` output.

    Returns only non-audio, non-hackrf entries because native HackRF is handled
    by `hackrf_sweep` in this app.
    """
    blocks: List[Dict[str, str]] = []
    cur: Optional[Dict[str, str]] = None

    for raw in (text or "").splitlines():
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

        key = m.group(1).strip().lower()
        cur[key] = m.group(2).strip()

    if cur is not None:
        blocks.append(cur)

    devices: List[Dict[str, Any]] = []
    for info in blocks:
        driver = str(info.get("driver") or "").strip()
        if not driver:
            continue

        dlow = driver.lower()
        serial = str(info.get("serial") or "").strip()
        label = str(info.get("label") or info.get("hardware") or driver).strip()
        label_l = label.lower()

        # Ignore audio backends and Soapy-hackrf entries in UI/device maps.
        if dlow == "audio" or "audio" in dlow or "audio" in label_l:
            continue
        if "hackrf" in dlow or "hackrf" in label_l:
            continue

        args = f"driver={driver}"
        if serial:
            args += f",serial={serial}"

        devices.append(
            {
                "label": label,
                "args_str": args,
                "driver": driver,
                "driver_lower": dlow,
                "serial": serial,
                "info": info,
            }
        )

    return devices


def find_soapy_util() -> Optional[str]:
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

    return util


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


def list_hackrf_devices(timeout_s: float = 5.0) -> List[Dict[str, str]]:
    exe = shutil.which("hackrf_info")
    if not exe:
        return []

    try:
        res = _run_text([exe], timeout_s=timeout_s)
    except Exception:
        return []

    return parse_hackrf_info_output(res.stdout or "")


def list_soapy_devices(timeout_s: float = 8.0) -> List[Dict[str, Any]]:
    util = find_soapy_util()
    if not util:
        return []

    try:
        res = _run_text([util, "--find"], timeout_s=timeout_s)
    except Exception:
        return []

    return parse_soapy_find_output(res.stdout or "")

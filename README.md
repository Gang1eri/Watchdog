# Watchdog (Multi-SDR RF Tripwire)

Watchdog is a lightweight SDR “RF tripwire” that monitors one or more frequency ranges and alerts when signals meet your configured parameters (threshold + persistence / hold time).

It supports running multiple SDRs at once (example: 2× HackRF + 1× bladeRF), each assigned to its own band.

## Supported platforms

### ✅ Officially supported (v1.3)
- **DragonOS Pi64 / Debian-based Linux on Raspberry Pi 5** (and similar Debian-based Linux)

### ⚠️ Windows (possible, not the main target)
Windows can work, but SDR drivers + Soapy stacks have a lot of gotchas.  
A guide is included here: `docs/install-windows.md`. If you get stuck, open an issue or message me.

---

## Quick start (DragonOS / Debian / Raspberry Pi OS)

### Option A (recommended): “Professional” installer

This is the easiest path and is what most users should do:

```bash
sudo apt update
sudo apt install -y git

cd ~
git clone https://github.com/Gang1eri/Watchdog.git
cd Watchdog

chmod +x install_dragonos.sh
./install_dragonos.sh
```

After install, launch **Watchdog** from your app menu (search “Watchdog”), or run:

```bash
cd ~/Watchdog
source .venv/bin/activate
python3 main.py
```

Update later:

```bash
cd ~/Watchdog
git pull
./install_dragonos.sh
```

### Option B: Manual install (advanced / troubleshooting)

Use this only if you want to understand each step or you’re troubleshooting.

#### 1) Install system dependencies (APT)

On Debian/DragonOS/Raspberry Pi OS, we install **PyQt5 via apt** (not pip) for best compatibility.

```bash
sudo apt update
sudo apt install -y   git   hackrf   python3 python3-venv python3-pip   python3-pyqt5 python3-pyqt5.qtmultimedia   python3-pyqtgraph
```

Sanity check:

```bash
hackrf_info
hackrf_sweep -h
python3 -c "from PyQt5 import QtWidgets; print('PyQt5 OK')"
python3 -c "from PyQt5.QtMultimedia import QSoundEffect; print('QtMultimedia OK')"
```

#### 2) Clone the repo

```bash
cd ~
git clone https://github.com/Gang1eri/Watchdog.git
cd Watchdog
```

#### 3) Create a venv (IMPORTANT on Linux)

Because PyQt5 is installed with **apt** (system-wide), your venv must include system site-packages or you may see:

`ModuleNotFoundError: No module named 'PyQt5'`

```bash
python3 -m venv .venv --system-site-packages
source .venv/bin/activate
python -m pip install -U pip
```

#### 4) Install pip dependencies

```bash
pip install -r requirements-debian.txt
```

#### 5) Run

```bash
python3 main.py
```

---

## Desktop launcher (Linux)

If you used `./install_dragonos.sh`, the desktop launcher is installed automatically.

If you want to verify it:

```bash
ls -l ~/.local/share/applications/watchdog.desktop
```

---

## Troubleshooting (Linux)

### `ModuleNotFoundError: No module named 'PyQt5'`

Fix (install system PyQt5 + recreate venv correctly):

```bash
sudo apt install -y python3-pyqt5 python3-pyqt5.qtmultimedia
cd ~/Watchdog
deactivate 2>/dev/null || true
rm -rf .venv
python3 -m venv .venv --system-site-packages
source .venv/bin/activate
pip install -r requirements-debian.txt
python3 main.py
```

### HackRF permission issues

If `hackrf_info` requires sudo, you may need udev rules. See:
- `docs/install-pi-bookworm.md`

---

## Docs
- Linux / Raspberry Pi OS Bookworm: `docs/install-pi-bookworm.md`
- Windows (not the main target): `docs/install-windows.md`

---

## Versioning / Releases
This repo uses tags for releases. Example:

```bash
git pull
git checkout v1.3
```

---

## License
MIT (see `LICENSE`)

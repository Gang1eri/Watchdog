# Watchdog (Multi-SDR RF Tripwire)

Watchdog is a lightweight SDR “RF tripwire” that monitors one or more frequency ranges and alerts when signals meet your configured parameters (threshold + persistence / hold time).

It supports running multiple SDRs at once (example: 2× HackRF + 1× bladeRF), each assigned to its own band.

## Supported platforms

### ✅ Linux (recommended)
- **DragonOS Pi64 (Raspberry Pi)**
- **DragonOS “regular” (x86_64, Lubuntu/Ubuntu-based)**
- **Ubuntu / Lubuntu / Debian / Raspberry Pi OS (Debian-based)**

> This project targets Debian/Ubuntu-family Linux first (APT + Python3).

### ⚠️ Windows (possible, not the main target)
Windows can work, but SDR drivers + Soapy stacks have a lot of gotchas.  
A guide is included here: `docs/install-windows.md`. If you get stuck, open an issue or message me.

---

## Quick start (Linux)

### Option A (recommended): One-command-ish installer

```bash
sudo apt update
sudo apt install -y git

cd ~
git clone https://github.com/Gang1eri/Watchdog.git
cd Watchdog

chmod +x install-linux.sh
./install-linux.sh
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
./install-linux.sh
```

#### Legacy / backwards-compatible installer name
If you see docs that say `./install_dragonos.sh`, that should still work too (it can be a tiny wrapper that calls `install-linux.sh`).

---

## Desktop launcher + desktop shortcut (Linux)

### App menu launcher
`install-linux.sh` installs a per-user launcher at:

```bash
~/.local/share/applications/watchdog.desktop
```

If clicking the app menu icon does nothing, check the log:

```bash
tail -n 200 ~/watchdog_menu.log
```

### Desktop shortcut (icon on Desktop)
Copy the launcher onto your Desktop:

```bash
mkdir -p ~/Desktop
cp ~/.local/share/applications/watchdog.desktop ~/Desktop/Watchdog.desktop
chmod +x ~/Desktop/Watchdog.desktop
```

If double-click doesn’t launch it, right-click the icon → **Allow Launching**.

---

## Option B: Manual install (advanced / troubleshooting)

Use this only if you want to understand each step or you’re troubleshooting.

### 1) Install system dependencies (APT)

```bash
sudo apt update
sudo apt install -y   git   python3 python3-venv python3-pip   python3-pyqt5 python3-pyqt5.qtmultimedia   python3-pyqtgraph
```

HackRF tools package name can vary by distro. Try:

```bash
sudo apt install -y hackrf || sudo apt install -y hackrf-tools
```

Sanity check:

```bash
hackrf_info
hackrf_sweep -h
python3 -c "from PyQt5 import QtWidgets; print('PyQt5 OK')"
python3 -c "from PyQt5.QtMultimedia import QSoundEffect; print('QtMultimedia OK')"
```

### 2) Clone the repo

```bash
cd ~
git clone https://github.com/Gang1eri/Watchdog.git
cd Watchdog
```

### 3) Create a venv (IMPORTANT on Linux)

Because PyQt5 is installed with **apt** (system-wide), your venv must include system site-packages or you may see:

`ModuleNotFoundError: No module named 'PyQt5'`

```bash
python3 -m venv .venv --system-site-packages
source .venv/bin/activate
python -m pip install -U pip
```

### 4) Install pip dependencies

Use whatever Linux requirements file your repo provides (commonly `requirements-debian.txt`):

```bash
pip install -r requirements-debian.txt
```

### 5) Run

```bash
python3 main.py
```

---

## Troubleshooting (Linux)

### App menu click does nothing
Check:

```bash
tail -n 200 ~/watchdog_menu.log
```

### `env: 'bash\r': No such file or directory`
This means a `.sh` file has Windows (CRLF) line endings. The repo should prevent this via `.gitattributes`, but if you see it:

```bash
sudo apt install -y dos2unix
cd ~/Watchdog
dos2unix packaging/linux/*.sh install*.sh
```

Then rerun:

```bash
./install-linux.sh
```

### `ModuleNotFoundError: No module named 'PyQt5'`
Recreate the venv with system site packages:

```bash
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
- `docs/install-debian-pi.md`

---

## Docs
- Linux / Raspberry Pi OS Bookworm: `docs/install-debian-pi.md`
- Windows (not the main target): `docs/install-windows.md`

---

## License
MIT (see `LICENSE`)

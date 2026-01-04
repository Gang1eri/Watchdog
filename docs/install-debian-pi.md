# Install (Debian, DragonOS, Pi OS Bookworm, 64-bit)

These instructions assume Debian 12 or 13 based systems like Pi OS and DragonOS **with Desktop**.

## 1) System prerequisites

Update OS:

```bash
sudo apt update
sudo apt full-upgrade -y
sudo reboot
```

## 2) Install/update git and clone Watchdog from the repo:

```bash
sudo apt update
sudo apt install -y git

cd ~
git clone https://github.com/Gang1eri/Watchdog.git
cd Watchdog

chmod +x install_dragonos.sh
./install_dragonos.sh

```

## 3) Launch the application from the terminal

```bash
cd ~/Watchdog
source .venv/bin/activate
python3 main.py

```

## 4) Desktop shortcut (optional)

This repo includes Linux launcher assets under `packaging/linux/`.

- `packaging/linux/run_watchdog.sh`
- `packaging/linux/HackRF-Watchdog.desktop`

**Note:** edit `Exec=` and `Path=` in the `.desktop` file to match your username and install folder.



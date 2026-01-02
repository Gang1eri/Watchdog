@echo off
setlocal
cd /d "%~dp0"

set "POTHOS=C:\Program Files\PothosSDR"
set "SOAPY_SDR_PLUGIN_PATH=%POTHOS%\lib\SoapySDR\modules0.8"

REM IMPORTANT: Do NOT add %POTHOS%\bin to PATH.
REM It can break hackrf_sweep by overriding libusb DLLs.

".venv\Scripts\python.exe" main.py

endlocal

@echo off
rem Creates a test folder inside this repo (testdata) and lists it in shares.txt.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0make-testdata.ps1"

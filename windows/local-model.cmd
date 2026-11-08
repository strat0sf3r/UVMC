@echo off
rem Runs a model on this PC's graphics card for UVMC (llama.cpp, no account needed).
rem   local-model              set it up: download, pick a model for your card, start it, connect UVMC
rem   local-model -Model qwen3-4b    switch to another model (the old one is deleted)
rem   local-model -Status | -Start | -Stop | -Remove
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0local-model.ps1" %*
if not "%~1"=="-Start" pause

@echo off
rem Runs models on this PC's graphics card for UVMC (llama.cpp, no account needed).
rem   local-model              set it up: download, pick a model for your card, start it, connect UVMC
rem   local-model -List            the models you can add, what each is good and bad at
rem   local-model -Add gemma4-e4b  download another one (the panel picks which one UVMC uses)
rem   local-model -Delete <model>  delete one; local-model -Test [model] checks it with UVMC's tools
rem   local-model -Status | -Start | -Stop | -Remove
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0local-model.ps1" %*
if not "%~1"=="-Start" pause

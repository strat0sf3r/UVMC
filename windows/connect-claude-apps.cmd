@echo off
rem Lets the Claude apps on this PC (Claude Code, Claude Desktop) use UVMC as a tool.
rem   connect-claude-apps           add UVMC to them
rem   connect-claude-apps -Remove   take it out again
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0connect-claude-apps.ps1" %*

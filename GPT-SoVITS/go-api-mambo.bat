@echo off
title GPT-SoVITS Local API Server
cd /d "%~dp0"
echo Starting GPT-SoVITS API on http://127.0.0.1:9880 ...
runtime\python.exe api.py -a 127.0.0.1 -p 9880
pause

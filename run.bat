@echo off
setlocal
REM Activate in current folder
py -m pip install -r requirements.txt
py bot.py

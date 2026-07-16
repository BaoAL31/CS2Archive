@echo off
cd /d D:\Projects\CS2Archive
set PYTHONPATH=.
C:\Users\jembo\anaconda3\envs\cs2archive\python.exe scripts\pipeline.py --backlog backlog\2395001-spirit-vs-falcons-iem-cologne-major\medium\magixx-mirage-2395001-spirit-vs-falcons-iem-cologne-major.json --batches 1 > .pipeline\magixx_rerender.log 2>&1
echo DONE_EXIT=%ERRORLEVEL% >> .pipeline\magixx_rerender.log

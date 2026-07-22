@echo off
cd /d D:\Projects\CS2Archive
echo START > scripts\_check_hf_out.txt
echo PYTHONPATH=. >> scripts\_check_hf_out.txt
C:\Users\jembo\anaconda3\envs\cs2archive\python.exe -c "print('hello from python')" >> scripts\_check_hf_out.txt 2>&1
echo END >> scripts\_check_hf_out.txt

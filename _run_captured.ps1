$env:PYTHONPATH="."
$output = & "C:\Users\jembo\anaconda3\envs\cs2archive\python.exe" scripts/pov/pipeline.py --backlog "backlog/2396005-100-thieves-vs-falcons-blast-bounty-2026-season-2/high/poiii-dust2-2396005-100-thieves-vs-falcons-blast-bounty-2026-season-2.json" 2>&1
$output | Out-File -FilePath "D:\Projects\CS2Archive\poiii_captured.log" -Encoding utf8 -Append

import subprocess, sys

PY = r"C:\Users\jembo\anaconda3\envs\cs2archive\python.exe"
ARGS = [
    PY, "scripts/pipeline.py",
    "--backlog", r"backlog\2395001-spirit-vs-falcons-iem-cologne-major\medium\magixx-mirage-2395001-spirit-vs-falcons-iem-cologne-major.json",
    "--batches", "1",
]
LOG = r"D:\Projects\CS2Archive\.pipeline\magixx_rerender.log"
with open(LOG, "w") as f:
    f.write("")  # truncate
p = subprocess.Popen(
    ARGS,
    stdout=open(LOG, "a"),
    stderr=subprocess.STDOUT,
    cwd=r"D:\Projects\CS2Archive",
    creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
    close_fds=True,
)
print(f"launched detached pid={p.pid}")
sys.exit(0)

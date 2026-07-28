"""Run pipeline script with proper __file__."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.argv = ["scripts/pov/pipeline.py", "--backlog", sys.argv[1]]
with open("scripts/pov/pipeline.py") as f:
    exec(f.read())

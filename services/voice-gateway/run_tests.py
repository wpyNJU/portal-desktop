"""Offline regression tests: no credentials, microphone, or provider calls."""
from pathlib import Path
import subprocess
import sys

root=Path(__file__).resolve().parent
tests=(
    'test_barge_in.py',
    'test_report_priority.py',
    'test_agent_handoff.py',
    'test_speech_output.py',
    'test_task_titles.py',
    'test_task_reports.py',
    'test_task_resilience.py',
    'test_background_streaming.py',
    'test_query_reuse.py',
    'test_report_prefetch.py',
    'test_report_edges.py',
    'test_agent_endpoint.py',
    'test_turn_scheduler.py',
)
for name in tests:
    subprocess.run([sys.executable,str(root/name)],cwd=root,check=True,timeout=30)
print(f'PASS: {len(tests)} offline regression scripts')

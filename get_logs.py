# ruff: noqa: BLE001
import json
import urllib.request

url = 'https://api.github.com/repos/Roastedpotato21/pulse/actions/runs?per_page=5'
req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
with urllib.request.urlopen(req) as resp:
    data = json.loads(resp.read().decode('utf-8'))

for run in data.get('workflow_runs', []):
    if run['conclusion'] == 'failure':
        print('Fetching jobs for run', run['id'])
        jobs_req = urllib.request.Request(run['jobs_url'], headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(jobs_req) as jresp:
            jobs_data = json.loads(jresp.read().decode('utf-8'))
        for job in jobs_data.get('jobs', []):
            if job['conclusion'] == 'failure':
                print(f"Job {job['name']} failed.")
                logs_url = f"https://api.github.com/repos/Roastedpotato21/pulse/actions/jobs/{job['id']}/logs"
                try:
                    log_req = urllib.request.Request(logs_url, headers={'User-Agent': 'Mozilla/5.0'})
                    with urllib.request.urlopen(log_req) as lresp:
                        content = lresp.read().decode('utf-8')
                        lines = content.splitlines()
                        for i, line in enumerate(lines):
                            if 'FAILED tests' in line or '== FAILURES ==' in line or '== ERRORS ==' in line:
                                print('\n'.join(lines[max(0, i-5):i+50]))
                                break
                except Exception as e:
                    print('Failed to get log:', e)
        break
# ruff: noqa: BLE001


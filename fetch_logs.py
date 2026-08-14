import httpx

r = httpx.get('https://api.github.com/repos/Roastedpotato21/pulse/actions/runs?per_page=5')
data = r.json()
for run in data.get('workflow_runs', []):
  print('Run:', run['id'], run['status'], run['conclusion'], run['head_commit']['message'].splitlines()[0])
  for job in httpx.get(run['jobs_url']).json().get('jobs', []):
    if job['conclusion'] == 'failure':
      print('  Job:', job['name'], 'failed at step:', [s['name'] for s in job['steps'] if s['conclusion'] == 'failure'])

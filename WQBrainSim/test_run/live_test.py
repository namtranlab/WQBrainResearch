import argparse
import json
from pathlib import Path

from core import BrainClient, JobStore, credentials, run
from core.models import payload, validate_alpha

parser = argparse.ArgumentParser(description="Run the example alpha using local testing credentials.")
parser.add_argument("--rerun", action="store_true", help="Create a new simulation attempt instead of reusing a saved job.")
args = parser.parse_args()
settings = json.loads(Path("example/settings.json").read_text())
alpha = json.loads(Path("example/alpha.json").read_text())
validate_alpha(alpha, settings)
print(json.dumps(payload(alpha["expression"], settings)), flush=True)
with BrainClient(credentials=credentials, account="testing") as client, JobStore("test_run/jobs.db") as store:
    job = run(alpha["expression"], settings, description=alpha["description"],
              client=client, store=store, deadline=600, rerun=args.rerun)
    print(json.dumps(job.to_dict(), indent=2), flush=True)
    Path("test_run/example-alpha-result.json").write_text(json.dumps(job.to_dict(), indent=2))

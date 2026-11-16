# CarePlix Voice — working agreement

## Ship every change all the way (owner's standing instruction)

After a change is done:

1. Run the checks: `uv run ruff check . && uv run ruff format --check . && uv run pytest -q` and
   `npm --prefix apps/web run build`.
2. Commit, push, open the PR, and once CI is green **merge it into `main` yourself**. No need to ask
   for merge approval.
3. Then give the owner the exact command to deploy from their Mac, in its own `bash` block, and say
   which commit the build should report. The cloud session has no Google Cloud login, so the owner
   runs the deploy:

   ```bash
   cd ~/Desktop/CarePlix_Voice && git switch main && git pull && ./infra/gcp/deploy-labs.sh build && ./infra/gcp/deploy-labs.sh deploy
   ```

   - The build ends with `built <short sha>`; it must match the merge commit on `main`.
   - `deploy` deploys the tag `build` recorded in `.deploy-labs-tag` and prints `deploying <short sha>`,
     so the two steps match even if another session moves this checkout in between (it says so when
     that happens). It stops before changing anything if those images aren't in the registry.
     `TAG=<sha>` overrides both steps.
   - Add `./infra/gcp/deploy-labs.sh setup` first when a change adds Secret Manager entries.
4. Finish with what to test in the Studio (careplix-labs) and how to get worker logs if it fails:

   ```bash
   gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="careplix-worker"' --project careplix-labs --freshness=1h --limit 100 --format='value(timestamp,severity,textPayload,jsonPayload.message)'
   ```

## Environment notes

- The owner's working clone is `~/Desktop/CarePlix_Voice` (it has the `.env`). Never ask for secrets
  in chat; the deploy script reads them from `.env` and Secret Manager.
- Everything is dev (careplix-labs) with synthetic data.
- Outbound calls go through the Twilio SIP trunk `careplix-voice.pstn.twilio.com`, with caller ID
  +1 312 755 8589. Only numbers under Connections → Approved destinations are dialled.

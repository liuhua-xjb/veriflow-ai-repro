# GitHub repository publication checklist

STATUS: NOT PUBLISHED. Public URL is not available yet.
Do not claim this directory has been uploaded to GitHub.

1. Confirm team ownership of all code, datasets, third-party notices
   and permission to expose the public repository. Remove anything
   that is not cleared for redistribution.
2. From this directory, check no .env, private Key, runtime SQLite DB,
   API token or personally identifying information is tracked.
3. On YOUR Windows PC, log into GitHub CLI (explicit human login):
   gh auth login --web
4. Verify the right account:
   gh auth status
5. Only after explicit permission for public publishing, run:
   git init
   git add .
   git commit -m "feat: reproducible offline VeriFlow HTTP demo"
   gh repo create veriflow-ai-repro --public --source . --remote origin --push
6. Confirm the real public URL using:
   gh repo view --json url,visibility
7. Visit that URL in a logged-out browser and inspect README and contents.
   Then fill the real link in the contest submission form.

If rights are NOT signed off, choose a private repo and grant reviewers
access through the platform's supported approach; do not claim private
files are publicly reproducible.

The archive you submit to the contest and the repo must describe the
actual implemented/verified scope. No model API calls were performed
in this E2E verification.

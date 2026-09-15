# CI status dashboard

Builds a static page showing which of your repositories have failing GitHub Actions workflows on their default branch.

GitHub notifies you about workflow runs you triggered yourself, and its Actions metrics show failure rates over time. Neither answers "which of my repositories
is red right now". This action does, as a page you check when you feel like it rather than a stream of notifications.

- One row per repository, grouped into failing, running, and passing.
- Only the default branch counts. Pull request runs are ignored.
- Repositories without workflow runs drop out by themselves, so there is no list to maintain.
- Private repository names are replaced with stable pseudonyms by default, because GitHub Pages sites are public.

## Set it up

1. Create a repository to host your dashboard. It has to be public unless you have a plan that includes access-controlled Pages.
2. Add this as `.github/workflows/ci-dashboard.yml`, with `YOUR-USERNAME` replaced, and push it.

   ```yaml
   name: CI dashboard

   on:
     schedule:
       - cron: "23 5 * * *"
     workflow_dispatch:

   permissions:
     contents: read
     pages: write
     id-token: write

   concurrency:
     group: pages
     cancel-in-progress: false

   jobs:
     build:
       runs-on: ubuntu-latest
       steps:
         - uses: YOUR-USERNAME/ci-status-dashboard@v1
           with:
             token: ${{ secrets.DASHBOARD_TOKEN }}
             anonymize-private: ${{ vars.ANONYMIZE_PRIVATE }}
             anonymize-salt: ${{ secrets.ANONYMIZE_SALT }}
         - uses: actions/upload-pages-artifact@v4
           with:
             path: _site

     deploy:
       needs: build
       runs-on: ubuntu-latest
       environment:
         name: github-pages
         url: ${{ steps.deployment.outputs.page_url }}
       steps:
         - id: deployment
           uses: actions/deploy-pages@v4
   ```

3. Create a token under Settings > Developer settings > Personal access tokens > Fine-grained tokens. Set **Repository access** to "All repositories" and
   **Repository permissions > Actions** to "Read-only". Metadata is added for you; nothing else is needed. Copy the token.
4. In the dashboard repository, go to Settings > Secrets and variables > Actions > **Secrets** and add two: `DASHBOARD_TOKEN` is the token from step 3, and
   `ANONYMIZE_SALT` is any long random string, say 32 characters from a password generator. Keep a copy of the salt: changing it relabels every private
   repository. With the CLI instead:

   ```
   gh secret set DASHBOARD_TOKEN --repo YOU/YOUR-DASHBOARD-REPO
   openssl rand -hex 32 | gh secret set ANONYMIZE_SALT --repo YOU/YOUR-DASHBOARD-REPO
   ```

5. In the same repository, open Settings > Pages and set **Source** to "GitHub Actions". The dropdown applies immediately, and there is no branch to pick. Skip
   this and the deploy job fails.
6. Open Actions > CI dashboard > **Run workflow**. The page appears at `https://YOU.github.io/YOUR-DASHBOARD-REPO/` and refreshes daily after that.

Runs will start failing when the token expires, and the page will show its stale warning.

The action only writes `index.html` into `_site`. Deploying it is up to you, so you can publish it somewhere other than GitHub Pages if you prefer.

## Private repositories

GitHub Pages sites are publicly accessible, so anyone who finds the URL can read the page. A dashboard listing your private repositories by name would leak
those names, and so would the workflow logs of a public dashboard repository.

With anonymizing on, which is the default, each private repository appears as a label like `private-3f9a`. The label comes from a keyed hash of the repository
name, so it stays the same between runs and cannot be reversed by guessing likely names. Links, workflow names, and the repository name itself are left out of
both the page and the logs. Public repositories are always shown in full.

To show everything as is, set a repository **variable** (not a secret) in your dashboard repository and re-run the workflow:

```
gh variable set ANONYMIZE_PRIVATE --body false --repo YOU/YOUR-DASHBOARD-REPO
gh workflow run ci-dashboard.yml --repo YOU/YOUR-DASHBOARD-REPO
```

Delete the variable to turn anonymizing back on. Any value other than `false` counts as on, so a typo leaves it running rather than exposing names. Turning it
back on replaces the page, but copies may already exist in caches and web archives.

## Inputs

| Input | Default | Description |
| --- | --- | --- |
| `token` | required | Fine-grained token with read-only Actions access to all of your repositories. |
| `anonymize-private` | `true` | Anything other than `false` hides private repository names and details. |
| `anonymize-salt` | none | Key for the pseudonyms. Required when anonymizing is on and you have private repositories. |
| `output-dir` | `_site` | Directory `index.html` is written to. |
| `stale-after-hours` | `48` | The page warns when it is older than this. `0` turns the warning off. |

## What counts as failing

The action looks at every active workflow in every repository you own that is not archived, disabled, or a fork. For each workflow it takes the most recent
usable run on the default branch, and the repository takes the worst state among them.

- Skipped, cancelled, and stale runs are passed over, so a cancelled rerun cannot hide an earlier failure.
- Runs triggered by pull requests are ignored, since a pull request from a fork can report the default branch's name as its own.
- Deleted and disabled workflows are ignored. Otherwise their last run would pin a repository to a state it can never leave.
- The run building the page is skipped in the dashboard's own repository, which would otherwise always be in progress at that moment. Its other runs still
  count, so that repository turns red like any other, but a failure in the deploy job only shows up on the next day's page.
- Anything waiting, queued, running, or needing approval counts as running rather than passing.
- A run result the action does not recognize counts as failing, on the grounds that a silent pass is the worse failure mode.
- GitHub's own workflows, such as Pages builds and Dependabot jobs, are included like any other.

An API error fails the whole run, so you get the previous page and a failed run rather than a quietly incomplete one.

## The schedule stops on its own

GitHub disables scheduled workflows in public repositories after 60 days without repository activity, and scheduled runs do not themselves count as activity.
A dashboard repository rarely gets commits, so expect this to happen. The page warns when it has not been refreshed for `stale-after-hours`. Re-enable the
workflow from the Actions tab of your dashboard repository.

Scheduled runs can also be delayed during busy periods, so the page is a snapshot from the last run rather than live data. Use **Run workflow** when you need it
current.

## Limits

- GitHub Actions only. Other CI services that report commit statuses are not included, because fine-grained tokens cannot be granted read access to checks.
- Repositories you own. Repositories belonging to organizations you are a member of are not listed.
- Current state only, with no history or trends.
- One state per repository, though the failing workflows are named.

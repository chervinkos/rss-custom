# Footwear Radar: stage 1 of the creative pipeline

A small program that runs on GitHub's servers every 30 minutes, with no server of your own and no card.
It reads your sources, keeps only drops, collabs, sales, outlets and reviews, removes duplicates, and
publishes the result at a web address you can open from any device.

After setup you get, at `https://YOUR-USERNAME.github.io/footwear-feed/`:

| Address | What it is |
|---|---|
| `/` | A page listing the latest items, with filters and search |
| `/feed.xml` | One combined RSS feed (subscribe to this in any reader, Make, Zapier, Pipedream) |
| `/feed-drop.xml`, `/feed-collab.xml`, `/feed-sale.xml`, `/feed-outlet.xml`, `/feed-review.xml` | The same, one label each, handy for routing |
| `/items.json` | All items as JSON: title, link, source, date, labels, image, prices, how many sources covered it |
| `/health.json` | Which sources worked on the last run |

## Setup (about 10 minutes)

1. **Create a GitHub account** at github.com/signup. Email only, no card.
2. **Create a repository:** click **+ → New repository**. Name it `footwear-feed`, choose **Public**,
   and click **Create repository**. (Pages needs a public repository on the free plan.)
3. **Upload the files.** On the new repository page click **uploading an existing file**, then drag in
   everything from the unzipped folder **except** the hidden `.github` folder:
   `build_feed.py`, `requirements.txt`, `sources.yml`, `keywords.yml`, `README.md`, and the `docs` folder.
   Click **Commit changes**.
4. **Add the schedule file.** Click **Add file → Create new file**. In the name box type exactly
   `.github/workflows/build.yml` (typing the slashes creates the folders). Open `.github/workflows/build.yml`
   from the zip in any text editor, paste its contents, and click **Commit changes**.
5. **Allow the workflow to save results:** **Settings → Actions → General → Workflow permissions →
   Read and write permissions → Save.**
6. **Run it once:** **Actions** tab → **Build feed** → **Run workflow**. Wait a minute or two for a green tick.
   A red cross means something failed; open the run to see why.
7. **Turn on the web page:** **Settings → Pages → Build and deployment → Source: Deploy from a branch →
   Branch: `main`, folder `/docs` → Save.** After a minute the page shows your address.
8. Open `https://YOUR-USERNAME.github.io/footwear-feed/`.

## Everyday use

- **Add or pause a source:** open `sources.yml` on GitHub, click the pencil icon, edit, **Commit changes**.
  The next run picks it up. Sources marked tier C start switched off.
- **Tune what counts as relevant:** edit `keywords.yml` the same way.
- **A source shows as failing on the page:** its feed address is probably wrong or the site blocks bots.
  Fix the address or set `enabled: false`. The other sources keep working.
- **Use it in Make, n8n, Zapier or Pipedream:** use a "watch RSS feed" step pointed at `/feed.xml`
  (or one of the per-label feeds). Each item carries its label as a category.

## Things to know

- GitHub does not guarantee exact timing. Runs can start a few minutes late.
- On public repositories GitHub switches off scheduled runs after 60 days with no repository activity.
  The workflow has a step meant to prevent that. Check the Actions tab once a month; if the schedule was
  disabled, press **Enable workflow**.
- Everything in the repository is public, including your source list. Never put passwords or API keys in it.
- Some sites block requests from GitHub's servers (Reddit sometimes does). They show up under
  "Source status" with an error.
- Sites with no feed are not supported yet; they are listed at the bottom of `sources.yml`.
- Items are kept for 14 days, up to 600 items (change `defaults` in `sources.yml`).

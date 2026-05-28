# Morgenbrief

A static morning briefing page that aggregates Norwegian news headlines and weather forecasts for three locations. Refreshes automatically every morning via GitHub Actions.

## What it shows

**News** (RSS-native headlines + descriptions, top 5 from each):
- VG
- E24
- DN
- kode24

**Weather** (via met.no, free, no API key required):
- Bønesberget, Bergen
- Holu gård, 3570 Ål
- Hamnavika, Tysnes

For each location: next ~12 hours hourly + 7-day forecast (rain mm, median day/night temperature).

## Setup (one time, ~10 minutes)

### 1. Create the repository

Create a new repository on GitHub (e.g. `morning-brief`). It can be private — GitHub Pages works with private repos on free accounts.

Copy these four files into the repo:

```
morning-brief/
├── fetch_brief.py
├── template.html
├── README.md
└── .github/
    └── workflows/
        └── daily.yml
```

You can either:
- **Upload via the GitHub web UI** (Add file → Upload files), or
- **Clone the empty repo, copy files in, commit, and push** from your Mac terminal.

### 2. Enable GitHub Pages

In your repo on github.com:

1. Click **Settings** (top right of the repo nav)
2. In the left sidebar, click **Pages**
3. Under **Source**, select **Deploy from a branch**
4. Branch: **main**, Folder: **/ (root)**
5. Click **Save**

After a minute, your page will be live at:
```
https://<your-username>.github.io/morning-brief/
```

### 3. Allow the workflow to push commits

In **Settings → Actions → General**:
- Scroll to **Workflow permissions**
- Select **Read and write permissions**
- Save

This lets the daily workflow commit the regenerated `index.html` back to the repo.

### 4. Run it once manually to verify

1. Go to the **Actions** tab in your repo
2. Click **Daily morning brief** in the left sidebar
3. Click **Run workflow** → **Run workflow** (green button)
4. Wait ~30 seconds, then refresh — you should see a green checkmark
5. Visit your GitHub Pages URL — the brief should be there

### 5. Add to home screen / bookmark

- **Mac**: bookmark the URL in Safari / Chrome
- **iPhone**: open the URL in Safari → share button → **Add to Home Screen**. You'll get an icon that opens the page fullscreen like an app.

## Scheduling notes

The workflow runs twice each morning (05:30 and 06:30 UTC) so that the page is fresh by 07:30 Norwegian time year-round regardless of daylight savings. Each run takes well under a minute. GitHub Actions free tier allows 2000 minutes/month; this uses around 60.

## Customizing

- **Different newspapers**: edit `NEWS_SOURCES` in `fetch_brief.py`
- **Different locations**: edit `LOCATIONS` (need lat/lon)
- **More/fewer articles per feed**: change `MAX_ARTICLES_PER_FEED`
- **Look and feel**: edit `template.html` (all CSS is inline)
- **Run time**: edit the `cron:` lines in `.github/workflows/daily.yml`

## When something breaks

- Check the **Actions** tab for red ❌ runs and read the logs
- If a single newspaper feed is down, its card will show "⚠️ Kunne ikke hente" — the rest of the page still works
- If the weather API is down, the same — news still shows
- If the workflow itself fails (e.g. GitHub permission issue), the previous `index.html` stays live until the next successful run

## Cost

Free. No API keys needed (met.no requires only a User-Agent string, which is set in the script — update it to point to your repo URL).

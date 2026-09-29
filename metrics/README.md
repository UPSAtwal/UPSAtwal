# metrics

Renders the stats card on this profile. `render.py` uses only the Python
standard library, and `.github/workflows/metrics.yml` runs it daily at 00:00 IST
with nothing but `git` and the runner's `python3`.

- **Data:** GitHub GraphQL and REST, WakaTime (last 7 days, languages only)
  and PageSpeed Insights for uday.codes. Any source that fails is left out of the card.
- **Colours:** sampled from the profile picture on every run. The photo is
  averaged in 4×4 blocks, then its darkest, middle and lightest tones become the
  palette. A dominant saturated hue, if there is one, becomes the accent. The
  last good palette is kept in `palette.json` in case sampling fails.
- **Push rhythm:** built from push timestamps only. No private repository names
  are ever written to the card.
- **Icons:** [Primer Octicons](https://github.com/primer/octicons) (MIT),
  embedded as path data in `icons.py`.

Run locally with `METRICS_TOKEN=$(gh auth token) python3 metrics/render.py`.

Secrets used: `METERICS_TOKEN` (a personal access token), `WAKATIME_TOKEN`,
`PAGESPEED_TOKEN`. The built-in `GITHUB_TOKEN` is the fallback.

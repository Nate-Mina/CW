# Clark &amp; Wolcott — Offline Site

A fully offline, dependency-free mirror of the Clark &amp; Wolcott Masonry and
Construction website. Photo galleries load from local image folders (no Wix
CDN calls at view time), so the site works without an internet connection.

## Pages
- **Home** — `index.html`
- **Residential Masonry** — `pages/residential-masonry.html` (61 photos)
- **Commercial Masonry** — `pages/c.html` (22 photos)
- **Home Remodeling** — `pages/home-remodeling.html` (11 photos)
- **Historic Restoration** — `pages/restoration.html` (8 photos)
- **Gallery** — `gallery-offline.html` (110 photos)
- Plus stub pages: `pages/blank-1.html` (Chimney), `pages/contact-now.html`
  (Contact), `pages/reviews.html` (Reviews), `pages/page.html`, `pages/gallery.html`.

Photos live in `images/<page-slug>/`:
`residential-masonry/`, `commercial-masonry/`, `home-remodeling/`,
`historic-restoration/`, `gallery/`.

## Run it locally

**Option A — static (no server needed):** just open `index.html` in a browser,
or serve the folder with any static server:
```
python3 -m http.server 8000
# then visit http://127.0.0.1:8000/
```

**Option B — `serve.py` (development):** a tiny server that proxies/caches the
few remaining Wix assets locally, and **auto-restarts on file changes** (no
manual refresh needed while editing).
```
python3 serve.py
# visit http://127.0.0.1:8000/
```
To rebuild the local asset cache from the live CDN: `python3 serve.py --warm`
(requires internet; the cache lives in `cdn/` which is git-ignored).

## Deploy to GitHub Pages
The site is static and uses **relative paths**, so it works unchanged under a
project subpath (`https://<user>.github.io/CW/`). Enable Pages in the repo
settings (Source: Deploy from a branch → `main`, `/root`). Push and wait
~1 minute for the first build. `.nojekyll` is included so Jekyll won't strip
asset directories.

## Notes
- The original site is a Wix export; its interactive runtime is dead offline,
  so nav links and photo grids are wired with a small client-side shim
  (`cw-navfix` in each page) that points links at the local files.
- `cdn/` is a regenerable cache and is intentionally **not** committed.

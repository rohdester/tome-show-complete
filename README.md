# The Tome Show — Complete Archive Feed

An unofficial RSS index combining the complete public web archive with the
official feed. It does not copy or re-host audio.

## Initial build

```sh
python3 generate_feed.py --full
```

Later runs omit `--full`; they retain the archive cache, refresh the newest
pages, and merge in all episodes currently exposed by the official RSS feed.

## Publish on GitHub Pages

1. Create a public GitHub repository and push these files to its `main` branch.
2. In **Settings → Pages → Build and deployment**, choose **GitHub Actions**.
3. Run **Update podcast feed** once under the repository's **Actions** tab.
4. Subscribe to `https://YOUR-USERNAME.github.io/REPOSITORY/feed.xml`.

The scheduled workflow updates the feed every six hours. The first committed
archive cache means routine runs only request the newest four archive pages.

This project is unaffiliated with Tome Show Productions and Podbean. If an
episode is removed by its publisher, its enclosure may stop working.

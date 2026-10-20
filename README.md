# Tender Portal

- `index.html` is the website (hosted on Vercel).
- `scraper.py` collects tenders into Supabase (runs on GitHub Actions every 6 hours).
- `.github/workflows/scrape.yml` is the schedule for the scraper.

No secret keys are stored in this repository. They live in GitHub Settings > Secrets.

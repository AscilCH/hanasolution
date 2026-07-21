# HanaSolution — Smart Contact Finder

Automated phone number discovery for your B2B CRM pipeline.

## How to Run Locally

```bash
pip install -r requirements.txt
python server.py
```

Then open http://localhost:8080

## How to Use

1. Upload your `.xlsx` file with company names
2. Click **Start Processing**
3. Watch as it finds phone numbers from multiple sources
4. Download the results as a new Excel file

## Search Sources

- Direct website guessing (.com.tn, .tn)
- DuckDuckGo search
- PagesJaunes.tn (Tunisian yellow pages)
- Ween.tn (business directory)
- Facebook pages

## Deployment

Deployed on [Render.com](https://render.com). Push to GitHub and connect to Render for automatic deploys.

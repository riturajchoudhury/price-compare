# Deploying to the Cloud (24/7 Access from Any Device)

This guide walks you through deploying your Streamlit + Playwright application using **Render** or **Railway**. Both services can automatically build the provided `Dockerfile` directly from your GitHub repository.

---

## Step 1: Push Code to GitHub

1. Create a new repository on [GitHub](https://github.com/new) (e.g. `price-compare`). Keep it **Private** or Public as you prefer.
2. In your local terminal (`C:\Users\Rituraj\Projects\price-compare`), initialize git, commit, and push:

```powershell
# Rename branch to main
git branch -M main

# Stage and commit all clean files
git add .
git commit -m "feat: setup docker containerization and cloud deployment"

# Link to your new GitHub repo (replace with your repo URL)
git remote add origin https://github.com/<YOUR_GITHUB_USERNAME>/price-compare.git

# Push your code
git push -u origin main
```

---

## Step 2: Deploy on Render (Recommended)

Render offers Docker web services and provides a public HTTPS URL (e.g. `https://price-compare.onrender.com`).

1. Log in to [Render](https://dashboard.render.com/).
2. Click **New +** -> **Web Service**.
3. Select **Build and deploy from a Git repository** and connect your `price-compare` repository.
4. Fill in the service details:
   - **Name**: `price-compare` (or any name you want)
   - **Region**: Choose the closest region (e.g., Singapore / Frankfurt)
   - **Environment**: **Docker** (Render will detect your `Dockerfile` automatically)
   - **Plan Type**: Free or Starter (Note: Starter with 1GB+ RAM is recommended for Playwright Chromium)
5. Under **Advanced** (Optional):
   - **Disk**: You can attach a persistent disk mounted at `/app/browser_session` (size: 1GB) so pincodes and session cookies persist across restarts.
6. Click **Deploy Web Service**.
7. Once the build finishes, Render will provide you with a live `https://...` link that you can open on your phone or any device!

---

## Step 3: Alternative — Deploy on Railway

Railway also builds Dockerfiles automatically and has generous trial credit:

1. Sign up at [Railway.app](https://railway.com/).
2. Click **New Project** -> **Deploy from GitHub repo**.
3. Select your `price-compare` repository.
4. Railway will automatically detect the `Dockerfile` and start building.
5. In **Settings** -> **Networking**, click **Generate Domain** to get your public HTTPS URL.

---

## Notes on Cloud Scraping & Bot Detection

- **Captchas on Datacenter IPs**: If Amazon or Flipkart show captchas in the cloud, you can attach a residential proxy (e.g., BrightData, Decaptcha, Webshare) to Playwright's `launch_persistent_context(proxy={"server": "..."})`.
- **Memory**: Running headless Chromium takes roughly 300MB–500MB of RAM during a scrape. Ensure your cloud instance has at least 1 GB of memory.

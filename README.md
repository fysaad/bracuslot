# BRACU Slot Finder

Paste a BRACU Wishlist / Self Registration / Advising schedule link, enter
earned credits and program, get your exact slot.

## Run locally

```bash
pip install -r requirements.txt
streamlit run app.py
```

## Deploy — Option A: Streamlit Community Cloud (recommended, free)

1. Push this folder to a GitHub repo (public or private).
2. Go to https://share.streamlit.io → "New app".
3. Pick the repo, branch `main`, and set **Main file path** to `app.py`.
4. Click Deploy. You'll get a URL like `https://<something>.streamlit.app`.
5. Any future `git push` to the repo auto-redeploys.

No Procfile or port config needed — Streamlit Cloud handles it natively.

## Deploy — Option B: Render

1. Push this folder to GitHub.
2. On Render: New → Web Service → connect the repo.
3. **Root Directory:** the folder containing `app.py` (leave blank if it's
   the repo root — same gotcha you hit with the Free Room Finder app).
4. **Build Command:** `pip install -r requirements.txt`
5. **Start Command:** `streamlit run app.py --server.port $PORT --server.address 0.0.0.0`
   (already saved in the included `Procfile`, but Render's dashboard field
   takes priority if both exist — set it explicitly to be safe).
6. Environment: Python 3. No extra env vars needed.
7. Deploy. Render assigns a `.onrender.com` URL.

## Files

- `app.py` — the Streamlit app
- `requirements.txt` — Python deps
- `Procfile` — start command for Render
- `.streamlit/config.toml` — server + theme config

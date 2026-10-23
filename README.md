# SCAIe: SCA project idea generator

SCAIe is a Streamlit app that gives TP students tailored project ideas for the Sustainability & Climate Action group project (TGS1001/TGS1003). Students pick their diploma, a challenge theme, an optional problem and a deliverable format. Claude then searches the web for real Singapore evidence and returns 3 ideas laid out like the brief's 4 steps. Students can download the ideas as a Word file.

## For colleagues: what you can edit

| File | What it controls |
|---|---|
| `prompt.md` | Claude's instructions: tone, rules, the brief summary. Plain English. Keep the `{placeholders}`. |
| `options.yaml` | Dropdown lists (diplomas, themes, deliverables) and settings (ideas per run, daily limit, model). |

Edit either file on GitHub and commit. The live app picks up the change within a minute; you don't need to touch the code.

## Run it on your laptop

```bash
pip install -r requirements.txt
cp .streamlit/secrets.toml.example .streamlit/secrets.toml   # then paste your Anthropic API key in
streamlit run app.py
```

If the `[auth]` section is left out of `secrets.toml`, the app runs without login.

## Deploy (Streamlit Community Cloud)

1. Push this folder to a GitHub repo. `secrets.toml` is git-ignored, so it won't be uploaded.
2. On share.streamlit.io, create an app pointing at `app.py`.
3. In **App settings → Secrets**, paste the contents of your `secrets.toml`.

## Turn on login (Clerk)

The app uses Streamlit's built-in login, with Clerk as the sign-in provider (OpenID Connect).

1. In the Clerk dashboard, go to **OAuth applications → Add application**. Turn on the `openid`, `email` and `profile` scopes.
2. Add the redirect URI `https://YOUR-APP.streamlit.app/oauth2callback`. Add `http://localhost:8501/oauth2callback` too if you test locally.
3. Copy the **Client ID**, the **Client Secret** (it's shown only once) and the **Discovery URL** into the `[auth]` section of the secrets.
4. Set who can get in under `[access]`. You can list whole domains (e.g. every `@student.tp.edu.sg` address), single emails, or both.

A student who signs in with an email that isn't on the list sees a "no access" message.

## Costs and limits

- Each generation runs up to `max_web_searches` web searches (US$10 per 1,000) plus model tokens.
- `max_generations_per_day` caps each student. The counter resets when the app restarts.
- An evidence link is shown only if Claude actually found that page in search. Any other link is removed, and the evidence point is marked "verify this".

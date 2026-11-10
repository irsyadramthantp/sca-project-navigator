# SCAIe: SCA project idea generator

SCAIe is a Streamlit app that gives TP students tailored project ideas for the Sustainability & Climate Action group project. Students pick their diploma, a challenge theme, an optional problem and a prototype type (physical or digital). Gemini then searches Google for real Singapore evidence and returns 3 ideas laid out like the brief's 4 steps. Students can download the ideas as a Word file.

## For colleagues: what you can edit

| File | What it controls |
|---|---|
| `prompt.md` | Gemini's instructions: tone, rules, the brief summary. Plain English. Keep the `{placeholders}`. |
| `options.yaml` | Dropdown lists (diplomas, themes, prototype types) and settings (ideas per run, daily limit, model). |

Edit either file on GitHub and commit. The live app picks up the change within a minute; you don't need to touch the code.

## Run it on your laptop

```bash
pip install -r requirements.txt
cp .streamlit/secrets.toml.example .streamlit/secrets.toml   # then paste your Gemini API key in
streamlit run app.py
```

If the `[auth]` section is left out of `secrets.toml`, the app runs without login.

## Deploy (Streamlit Community Cloud)

1. Push this folder to GitHub. `secrets.toml` is git-ignored, so it won't be uploaded.
2. On share.streamlit.io, create an app pointing at `app.py`.
3. In **App settings → Secrets**, paste the contents of your `secrets.toml`.

## Login (Auth0)

The app uses Streamlit's built-in login, with Auth0 as the sign-in provider. Students log in with an emailed code, so there are no passwords to manage.

1. In Auth0, go to **Applications → Create Application** and choose **Regular Web Application**.
2. In its settings, set both **Allowed Callback URLs** and **Allowed Logout URLs** to `https://YOUR-APP.streamlit.app/oauth2callback`, then save.
3. Turn on **Authentication → Passwordless → Email** and enable it for the app.
4. Under the app's **Connections** tab, leave only **email** on.
5. Under **Authentication → Authentication Profile**, choose **Identifier First**.
6. Under **Branding → Email Provider**, connect your own SMTP sender (e.g. a Gmail app password). Auth0's built-in sender allows only 10 emails a minute.
7. Copy the **Client ID**, the **Client Secret** and your Auth0 domain into the `[auth]` section of the secrets.
8. Set who can get in under `[access]`. You can list whole domains, single emails, or both.

A student who logs in with an email that isn't on the list sees a "no access" message.

## How the AI part works

Each generation makes two Gemini calls:
1. Gemini searches Google and writes the ideas, following `prompt.md`.
2. Gemini sorts that write-up into the fixed layout students see.

An evidence point gets a link only if it's backed by a page Gemini actually found in search. Anything else is marked "verify this". Google requires search-grounded answers to show its search suggestions, so the results page includes them.

## Costs and limits

- Google Search grounding on Gemini 3 models comes with a free monthly allowance, then is charged per search query. See Google's Gemini API pricing page for current rates.
- `max_generations_per_day` caps each student. The counter resets when the app restarts.

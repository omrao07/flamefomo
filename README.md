# FLAME FOMO

A campus events app: five picks a day, no class clashes, and seat-limited sign-ups you cannot miss.

```
flame-fomo/
  app.py            the brain (Flask server + database + sign in)
  public/index.html the face (the whole website, one file)
  requirements.txt  list of Python helpers Vercel installs for you
  .env.example      list of secret settings you need to fill in
```

## Why a Postgres database?

Vercel is like a hotel room: it cleans out your stuff all the time. A SQLite file would vanish
and everyone's registrations would disappear. So online we use a free Postgres database (Neon).
On your own laptop the app still uses a simple file, so you do not need anything extra to try it.

---

## Part 1. Try it on your laptop (5 minutes, optional)

1. Install Python 3.12 from python.org.
2. Open a terminal inside the `flame-fomo` folder.
3. Type these one at a time:
   ```
   python -m venv .venv
   source .venv/bin/activate        (Windows: .venv\Scripts\activate)
   pip install -r requirements.txt
   DEV_SHOW_CODE=1 python app.py       (Windows PowerShell: $env:DEV_SHOW_CODE=1; python app.py)
   ```
4. Open http://localhost:5000
5. Type `yourname@flame.edu.in`, press **Email me a code**. The code shows on screen (test mode).

## Part 2. Put it on GitHub

1. Make a free account at github.com.
2. Click **+** (top right) then **New repository**. Name it `flame-fomo`. Leave it **Private**. Click **Create**.
3. On the next page click **uploading an existing file**.
4. Drag in everything inside the `flame-fomo` folder (the `public` folder too).
   Do NOT upload a file called `.env`.
5. Click **Commit changes**.

## Part 3. Make the database (Neon, free)

1. Go to neon.tech, sign up, click **Create project**. Name it `flame-fomo`.
2. On the project page click **Connect**, and copy the long text that starts with `postgresql://`.
   Keep that tab open. This is your `DATABASE_URL`.

## Part 4. Put it on Vercel

1. Go to vercel.com, sign up with GitHub.
2. Click **Add New... > Project**, pick `flame-fomo`, click **Import**.
3. Before clicking Deploy, open **Environment Variables** and add these (name on the left, value on the right):

   | Name | Value |
   |---|---|
   | `SECRET_KEY` | any long random text (60+ letters and numbers) |
   | `DATABASE_URL` | the text you copied from Neon |
   | `ADMIN_EMAILS` | `tiyana.shah@flame.edu.in` |
   | `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `SMTP_FROM` | from your email provider (see below) |
   | `DEV_SHOW_CODE` | `1` only while testing, then delete it |

4. Click **Deploy**. When the confetti stops, click the picture of your site.
5. Visit `your-site.vercel.app/api/health`. You should see `{"database":"postgres","ok":true}`.

## Sending the 6-digit codes

The app sends email through SMTP. Easiest choices:
- **Ask FLAME IT** for an SMTP relay for `@flame.edu.in` senders.
- **Gmail** (for testing): turn on 2-step verification, create an *App password*,
  then `SMTP_HOST=smtp.gmail.com`, `SMTP_PORT=587`, `SMTP_USER=you@gmail.com`,
  `SMTP_PASS=<app password>`, `SMTP_FROM=you@gmail.com`.
- Until email works, set `DEV_SHOW_CODE=1` so you can still try the app. **Remove it before real students use it**,
  because anyone could then sign in as any student.

## Continue with Google (optional)

1. Google Cloud Console > APIs and Services > Credentials > **Create OAuth client ID** > Web application.
2. Under *Authorised JavaScript origins* add your Vercel address.
3. Copy the Client ID into the Vercel variable `GOOGLE_CLIENT_ID`, then redeploy.

## Organisers

Admins (emails in `ADMIN_EMAILS`) get an **Add event** tab automatically. To make someone an organiser,
run this once in Neon's **SQL Editor** (after they have signed in once):

```sql
UPDATE users SET role = 'organiser' WHERE email = 'someone@flame.edu.in';
```

## What is real and what is a preview

Real (saved on the server): sign in, events, Register (the last seat can only be taken once),
I am going, Skip, interests, busy time and classes, calendar, free windows, My plan, clash warnings.

Works inside the page: Ask FOMO (answers from your events and calendar), the map pins, your map position.

Preview only: the Gmail, Moodle and Google Calendar switches are remembered on the phone but do not
connect to those services yet. The source switches (Student clubs, Departments, Official) do filter events.

## If something looks wrong

- **"The server is missing its SECRET_KEY setting"**: add `SECRET_KEY` in Vercel, then Deployments > Redeploy.
- **Codes never arrive**: check the SMTP values, and look in the spam folder.
- **Everything is empty after a while**: the app adds sample events by itself when none are upcoming.
  Admins can also press **Add sample events** on the Add event tab.

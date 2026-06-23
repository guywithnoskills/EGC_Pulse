# Deploy EGC Pulse — step-by-step (no coding, nothing to install)

This guide takes you from the project to a **live web link** you can open in any
browser. You will **not** type any code and **not** install any app.

- **Time:** about 25–40 minutes (most of it is waiting on uploads/builds).
- **Cost:** free (Render's free plan — no credit card).
- **What runs where:** GitHub *stores* the files; Render *runs* the app and gives
  you the live link.

### Two promises
1. You only click and type names — no command line, no code.
2. Your secret keys and data stay **off the internet**. A ready-made folder named
   **`egc-pulse-deploy`** is already on your **Desktop** with only the files needed
   to deploy — it contains **no `.env` and no database**.

---

## Part 1 — Make two free accounts (~5 min)

**1. GitHub** (stores the project) — go to **github.com** → **Sign up** → email,
password, username → verify your email.

**2. Render** (runs the app, gives the live link) — go to **render.com** →
**Get Started** → **Sign up with GitHub** (one click, links the two) → **Authorize**.

---

## Part 2 — Put the project on GitHub (website method — no app to install)

**3. Create an empty project**
1. Sign in at **github.com**.
2. Top-right **+** → **New repository**.
3. Repository name: `egc-pulse`.
4. Choose **Private**.
5. Tick **Add a README file** (so it isn't empty).
6. Click **Create repository**.

**4. Open the upload page**
- On the new project page, click **Add file ▾** (next to the green **Code** button)
  → **Upload files**.

**5. Drag in the files**
1. Open the **`egc-pulse-deploy`** folder on your **Desktop**.
2. Select **both** things inside it — the **`render.yaml`** file *and* the **`demo`**
   folder (click one, hold **⌘ Cmd**, click the other).
3. Drag them onto the GitHub page where it says *"Drag files here."*
4. Wait until the page lists `render.yaml` and the `demo/...` files.
   - If one or two hidden files don't show up, that's fine — they're optional.

**6. Save**
- Scroll down, click **Commit changes**.

Your project is on GitHub. ✅ (No secrets uploaded — the Desktop folder has none.)

---

## Part 3 — Deploy on Render (~10 min)

**7. Start a Blueprint** — go to **dashboard.render.com** → **New +** (top right)
→ **Blueprint**.

**8. Connect your repository**
1. If asked, click **Configure GitHub / Connect** → choose your account →
   **Only select repositories** → tick **egc-pulse** → **Install / Save**.
2. Back on Render, find **egc-pulse** in the list → **Connect**.

**9. Apply**
1. Render automatically finds the included `render.yaml` and shows a service called
   **egc-pulse**. Name the blueprint if asked (anything is fine).
2. It lists secret fields (Reddit, X, Meta, TikTok, etc.). **Leave them all blank
   for now** — the app still works on the free keyless sources (News, Mastodon,
   Lemmy, Hacker News, and more). Add keys later only for sources you have access to.
3. Click **Apply** (or **Create Resources**).

**10. Wait for the build** — logs scroll for ~3–8 minutes. When you see a green
**Live** badge, it's deployed.

---

## Part 4 — Open your app

1. Near the top of the service page, click the link Render shows (like
   **`https://egc-pulse.onrender.com`**).
2. EGC Pulse opens — add a tracked term, pick a date range, click **Collect**.

> First open can take ~30–60s: on the free plan the app sleeps after 15 min idle
> and wakes on the next visit. That's normal.

---

## Good to know

**Keeping your data.** The free plan resets collected data when it sleeps/wakes.
To keep data permanently, upgrade to Render **Starter** (~$7/month) and add a disk —
the exact steps are in the comments at the bottom of `render.yaml`, and your
technical contact can flip it on in a minute.

**The link is public (no password).** Anyone with the link can open it. For an
internal tool, don't post the link publicly. Ask your technical contact to add a
simple password if you'll be sharing it.

**Making changes later.** Edit on github.com (or re-upload via **Add file → Upload
files**), click **Commit changes**, and Render re-deploys automatically.

**If a build fails.** Open the service in Render → **Logs** tab → copy the red
error lines and send them to your technical contact. Nothing on your computer is
affected; you can retry from Step 9.

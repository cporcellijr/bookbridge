---
hide:
  - toc
description: "Connect Fable to BookBridge with your Fable email and password."
---

# Fable

Fable is a tracker: BookBridge posts your progress and finished books to it. It never moves your place in other apps.

!!! warning "Unofficial"
    Fable has no public API, so BookBridge signs in with your Fable email and password the way Fable's own apps do, and uses the same app API they use. Fable can change that API at any time, so updates may stop until BookBridge is updated.

## 1. Turn it on (admin)

**Settings → Services → Fable**

<div class="bb-callouts" markdown>
1. **Enable**, then **Save Settings**.
</div>

## 2. Connect your account (each reader)

**Account → My Integrations → Fable**

<div class="bb-callouts" markdown>
1. **Enable** it for your account.
2. **Account email** and **Account password**: the ones you sign in to Fable with. BookBridge needs an email-and-password sign-in.
3. **Test** checks the sign-in without posting anything.
</div>

Then **Save integrations**.

## What BookBridge does on Fable

- **Matching:** Fable lists each edition as its own book, and its search rarely shows the one you added, so BookBridge first looks for the book on your own Fable lists (by ISBN, or by title and author). Only if it isn't there does it search Fable by ISBN, ASIN, then title and author. You can change the match from the book's page (**Fable → Change link**) with a Fable book URL, a book ID or search text.
- **Lists:** once you're past 2% the book moves to **Currently Reading**, and at 99% to **Finished**. A book that isn't on any of your lists yet goes on **Want to Read** when you start it. A book that is already **Finished** or **Did Not Finish** is never moved back.
- **Progress:** whole-percent progress is posted after you have made no new progress for **Wait before posting progress** (60 minutes by default); finishing a book posts at once.
- **Rating:** the book's Fable rating and number of reviews are shown on its card, like StoryGraph's and Goodreads'. BookBridge never posts a rating or review for you.

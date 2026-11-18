# How to mark the network sheet

[Documentation index](README.md) · [Repository home](../README.md)

Use the dated review sheet supplied for this round (the original was
`data/research/2026-10-14-network-review.csv`, never committed). Keep its matching row-key file;
[the commands reference](commands.md) covers generating and checking a replacement. Fill in the six
columns below and leave the rest alone. Only marked rows are applied by this import;
people already on the site stay there. For the normal site workflow, use the
[user guide](../USER-GUIDE.md). Case doesn't matter, and the words in the "Also accepted" column work too. A
plain misspelling ("colleauge") is read as the word it means and listed in the check's report;
anything less certain is reported for you to settle.

## The columns you fill in

| Column | What to put | Also accepted | If you leave it blank |
| --- | --- | --- | --- |
| **How well (W/T/K/N)** | `W` know well · `T` in touch · `K` know from before · `N` never contact | know well, in touch, know from before, never | Not marked as known |
| **Priority (1/2)** | `1` contact first · `2` contact next | first, next, p1, p2 | "I know them, but not now": on People; This week suggests them only after your list |
| **Who** | colleague · manager · recruiter · founder · peer · friend · in-house talent · introducer · hiring manager · other | former colleague, ex colleague, former manager, hired me, boss, agency, talent | Not recorded |
| **Channel** | call · WhatsApp · text · email · LinkedIn · in person | phone, wa, sms, e-mail, li, meet, coffee | The card shows no channel |
| **Ask** | catch-up · intro · availability | catch up, introduction, referral, view, inside view, advice, available | People you know get **catch-up**; recruiters get **availability** |
| **Notes** | Anything that helps the approach: how you know them, what you last talked about, what to avoid | Start with `private:` to keep the note off the site | Nothing added |

Catch-up asks how things are going; intro asks for a referral or introduction;
availability opens a contract/interim discussion. The site shows the current wording
from [config/outreach.yml](../config/outreach.yml); a prepared approach can supply a
fitted ask instead. These are talking points to mix in your own words, not an exact draft approval.

If the **Priority** column is missing entirely, recognized relationship, channel or
ask tags normally put a person in **Next** (except never-contact rows). This differs
from a blank priority cell in a present column. Check the agent's report before import.

## Rules of thumb

- **A row counts once it has any tag** (How well, Priority, Who, Channel or Ask). A note on its own
  is not enough: the check reports that row so you can add a tag.
- **One value per cell.** If you write two ("WhatsApp, email"), the first is used and the row is
  reported, so put your preferred one first.
- **`N` with a priority** is a contradiction, and the row is reported.
- **Don't re-sort or delete rows,** and don't change the `#` or LinkedIn columns. Each row's `#` is
  checked against its LinkedIn link; if they no longer match, the import stops rather than tag the
  wrong person. Moving or deleting other columns is fine. If Google Sheets loses the `#` heading,
  the check puts it back while the first column still holds the row numbers.
- **Adding someone not in the sheet:** add a row at the bottom with an empty `#`, their name, and
  their LinkedIn link if you have it, plus your tags.
- **Former colleagues from the employer in your profile:** say so in the note (its name or short
  form, for example "ex-XC"). This adds the obligations warning to their record.
- **Leave private topics out of the sheet entirely** (`private_note_topics` in `config/profile.yml`).
  If one appears in a note, that note is not processed or sent anywhere; you get the row number so
  you can remove it.
- **Notes go on the site** behind your sign-in, marked "Your note (date)", except `private:` notes.

## When you send it back

Bring the saved CSV to a session, together with short answers to these seven questions:
1. Which columns did you use, and what was missing?
2. How much effort was it? Would you rather do it in one sitting, or ten at a time on your phone?
3. Which groups weren't worth showing?
4. Keeping the list current: monthly, or only when someone becomes relevant?
5. What would get you to contact someone in a given week?
6. Are the tags right?
7. Is there anyone or anything never to suggest?

The agent checks the sheet first and reports counts and any rows to settle with you. Then it does a
dry run, and only then the live import
(steps).

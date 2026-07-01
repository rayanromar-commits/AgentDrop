# AgentDrop Growth Playbook — Scaling YouTube Shorts (2026)

The strategy this channel runs on. Written for the AITA/petty-revenge storytime lane
(@StoryDropper on YouTube, @storydropper1 on TikTok), posting 3×/day, 60s+ narrated
Reddit stories over background footage. Each rule maps to a real knob in `config.yaml`
or a prompt file so it's actionable, not abstract.

Sources: 2026 guides from miraflow, vidIQ, socialchamp, shortimize, opus.pro,
JoinBrands, influenceflow, fluxnote. See "Sources" at the bottom.

---

## 1. The algorithm rewards viewer response, not channel size

In 2026 the Shorts algorithm barely cares how big you are — it cares how viewers
*react* in the first seeding round. Ranking signals, in rough priority order:

1. **Watch-through / completion rate** — the single most important metric. Aim for **70%+**.
2. **Replays** — a Short looping is a strong satisfaction signal.
3. **Shares** — the fastest way out of your subscriber bubble (74% of Shorts views come from non-subscribers).
4. **Comments** — session-lengthening + a "people are arguing here" signal.
5. **Like-to-view ratio** and post-view behavior (did they keep watching your stuff / subscribe).

**What this means for us:** we already hold ~80% retention (measured), so our bottleneck
is **spread, not retention** — i.e. shares and comments. That's exactly why the hook prompt
optimizes for "plant the itch to weigh in" and the outro asks for comments. Keep that focus.

---

## 2. The hook is the whole ballgame (first ~1 second)

- The decision to keep watching or swipe is **reflexive, at or before the 1-second mark**.
  The old "first 3 seconds" rule was built for 2018–2022 long-form discovery.
- A spoken hook should land in ~**10–14 words** (fits our "6–16 words" rule in `processing/hook.py`).
- Best-converting hook shapes for our niche: **contrarian claim**, **mistake/warning**,
  **in-medias-res jolt** ("wait, WHAT?"), and a **side-to-take** framing (someone's clearly
  wrong / got what they deserved).
- If 3-second retention is consistently below 60%, the hook formula is stale — rotate it.

**Where it lives:** `processing/hook.py` SYSTEM_PROMPT. Already strong. Keep hooks
polarizing; do not sand the edges.

---

## 3. Titles + first on-screen frame = CTR (our current weak spot)

- Titles are a top-3 discovery/CTR lever. Proven 2026 formulas: **curiosity gap**,
  **specific number**, **warning pattern**, **transformation/achievement**, **versus frame**.
- Format that hits 8%+ CTR: `Hook/result + specific topic + (for [audience] / in 2026)`.
- The raw Reddit title is a *summary*, not a hook — it's the one asset our AI doesn't rewrite.

**Action (highest-leverage open item):** generate the YouTube/TikTok title with Claude the
same way we generate the spoken hook — a curiosity-gap, side-to-take line — instead of
`raw title + #Shorts` in `upload/metadata.py`. Keep #Shorts in the title, keep the r/sub credit.

---

## 4. Length: keep testing shorter against our 60s+ bet

- General 2026 guidance: **15–35s** often wins on completion rate; shorter Shorts frequently
  beat 60s ones on watch-through.
- **But** our data says our 60s+ storytime holds ~80% retention, and longer watch *time* per
  view helps session metrics. So our `min_word_count: 180` (≈70s) is a defensible, deliberate bet.
- **Don't treat it as settled.** Run a proper A/B: a batch of tight ~30–40s single-punchline
  stories vs the 60s+ format, compare completion% and shares. Let the data, not the guide, decide.

---

## 5. Posting cadence: consistency beats volume

- Sweet spot is **3–5 quality Shorts/day**; consistency matters more than raw volume.
- Channels posting consistently for 6 months see ~44% more overall growth; 12+/mo strongly
  beats 1–3/mo. We're at 3/day = ~90/mo, well into the "active channel" boost band.
- **The real risk for us is stock-out, not under-posting.** An empty queue breaks consistency
  overnight. Keep unused stories above `restock_threshold` (5) — currently 9. `unused_story_count()`
  is the source of truth; watch the Slack low-stock alert.

---

## 6. Repeatable format = bingeable channel

- Use a **recognizable, repeatable format** so a new viewer instantly "gets" the channel and
  binges 3–4 in a row (session time is a ranking signal). We have this: consistent captions,
  cold-open hook, one narrator per story, outro CTA. Don't drift the visual identity.
- **Stay in one lane.** The 2026-06-29 horror detour tanked (9–187 views vs 700–950 in-lane).
  The algorithm typecasts the channel; off-genre Shorts get mis-seeded. Horror stays paused.

---

## 7. Test one variable at a time

Creators who test intentionally grow ~2.5× faster. Don't change five things at once — you
learn nothing. Suggested cadence, one lever per ~1–2 week batch:

- **Batch A:** hook style (contrarian vs in-medias-res vs warning).
- **Batch B:** title formula (curiosity gap vs number vs versus-frame).
- **Batch C:** length (30–40s vs 60s+).
- **Batch D:** posting times.

Our `use_performance_weighting` ranker already biases subreddit selection by age-normalized
views + engagement — that's the measurement backbone. Log which variable each batch changed so
the ranker's signal is attributable.

---

## 8. Cross-platform compounding

- Posting Shorts **and** long-form grows subs ~3× faster — a future lever (e.g. weekly
  compilation of the top stories as a long-form upload) once the Shorts engine is steady.
- TikTok cross-posting (@storydropper1) is live in inbox/draft mode — same asset, second
  discovery surface, near-zero marginal cost. Keep it flowing.

---

## Scorecard — what to watch weekly

| Metric | Target | Where |
|---|---|---|
| Completion rate | ≥70% | YouTube Studio / Shorts retention |
| 3-sec retention | ≥60% | if lower → rotate hook formula |
| Shares per 1k views | trend up | the real spread signal for us |
| Comments per video | trend up | outro CTA is working? |
| Unused story queue | >5 | `unused_story_count()` / Slack alert |
| Posting consistency | 3/day, no gaps | scheduler + queue stock |

---

## Sources

- [YouTube Shorts Best Practices 2026 — miraflow](https://miraflow.ai/blog/youtube-shorts-best-practices-2026-complete-guide)
- [Shorts Algorithm 2026 — socialchamp](https://www.socialchamp.com/blog/youtube-shorts-algorithm/)
- [Shorts Retention Rate 2026 — shortimize](https://www.shortimize.com/blog/youtube-shorts-retention-rate)
- [Hook Formulas — opus.pro](https://www.opus.pro/blog/youtube-shorts-hook-formulas)
- [10 Best Practices — JoinBrands](https://joinbrands.com/blog/youtube-shorts-best-practices/)
- [Shorts + Long-form Strategy 2026 — influenceflow](https://influenceflow.io/resources/youtube-shorts-and-long-form-video-strategy-the-complete-2026-creators-guide-1/)
- [Title Formulas 2026 — fluxnote](https://fluxnote.io/guides/how-to-write-viral-youtube-titles-2026)
- [Viral Hooks — vidIQ](https://vidiq.com/blog/post/viral-video-hooks-youtube-shorts/)

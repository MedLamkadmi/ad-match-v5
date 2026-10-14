# Ad Match Tool — Architecture & Pipeline Specification

**Author:** Mohamed Lamkadmi — Loblaw Loyalty Team
**Date:** October 14, 2026
**Version:** 5.0 — FINAL (v4 + Stage 6 Learning loop)
**Status:** Design document — implementation in progress

**What changed in v5 (FINAL):** Stage 6 Learning loop added as a full section — corrections from the Review Queue go to memory and become rules for next week's run, with the golden-test guardrail. This is the complete final specification.

**What changed in v4:** the output format is now DECIDED — full list of 69-70 items in 5-group priority order, 4 sheets (Summary / Action List / Review Queue / Warnings), precomputed price gaps, embedded images, Ellie's decision column.

**What changed in v3:** the matching strategy is now DECIDED — Strategy B (Idea 2): generous rules find candidates, Gemini judges, confidence-triggered retry with embedding similarity. Idea 1 (pre-split easy/hard) is formally rejected. Everything else from v2 is unchanged.

**What changed in v2:** every correction from Mohamed's review is baked in. Where the v1 document misread his design (categories, no-image items, STRONG handling), v2 states his actual rules. Where the consultant critique found real gaps, v2 adds the fixes.

---

## 1. What the tool does

Every week, the tool compares a fixed list of target grocery items against all competitor flyers, finds the lowest comparable price for each item, and shows how Superstore compares — in one Excel file Ellie can read and verify.

**One sentence:** *Raddar gives us the flyers. ETL cleans the data. Rules + Gemini do the matching. Excel shows the answer.*

---

## 2. Pipeline overview

```
STAGE 1 — TARGET      Define which stores, which region, which flyers
    |
STAGE 2 — EXTRACT     Pull ALL items from ALL target flyers (Raddar API)
                       + completeness check with loud warning
    |
STAGE 3 — ETL          Filter categories, clean, fill missing info (Gemini),
                       normalize into structured fields, merge into ONE clean table
    |
STAGE 4 — MATCH        Layer 1 rules (candidates) -> Layer 2 Gemini (judgment)
                       -> Layer 3 retry (widen + embeddings) -> Layer 4 human review
    |
STAGE 5 — OUTPUT       One Excel row per list item: competitor + Superstore
    |
STAGE 6 — LEARNING     Ellie's corrections -> memory -> new rules -> better next week
```

**Three design goals, in this order:** (1) accurate, (2) fast and efficient, (3) cheap to run.

---

## 3. Stage 1 — Target definition

Before any extraction, the tool must know exactly WHAT to pull.

### 3.1 Target stores

A fixed list of the retailers to track (the 9 approved banners):

| Retailer | Role |
| --- | --- |
| Superstore | Our banner — the comparison baseline |
| Walmart | Competitor |
| No Frills | Competitor |
| Sobeys | Competitor |
| Metro | Competitor |
| FreshCo | Competitor |
| Food Basics | Competitor |
| Loblaws | Competitor |
| Costco | Competitor |

### 3.2 Region

Only flyers for the target region are pulled (the region the loyalty team tracks). Flyers from other regions are ignored.

### 3.3 Flyer window — the "starting this week" rule

**Only flyers STARTING this week are extracted.**

- A flyer is included if its validity starts today, tomorrow, Wednesday, or Thursday of this week (the run week).
- The flyer's duration does not matter — 7 days, 10 days, 14 days, all accepted.
- A flyer from LAST week is excluded — even if it is still active this Monday or Tuesday and stops soon after.

**The 2-week carry-over case (special case):**

- If a flyer started last week (Monday, Tuesday, or Wednesday) and its validity covers this WHOLE week, it is KEPT — because its prices are still live all week.
- If the same store has BOTH a carried-over flyer AND a new flyer this week with different items, BOTH flyers are taken.

**Why:** last week's flyers are already priced in. Matching against them creates stale, wrong comparisons. But a flyer still valid all week is a live price, not a stale one.

---

## 4. Stage 2 — Extraction (Raddar API)

Pull **everything** from every target flyer — no filtering yet.

For each flyer item, extract:

- Title / name
- Price
- Image (URL)
- Page number in the flyer
- Validity dates (start and end)
- Category (as given by Raddar)
- Store / banner

**Output of this stage:** one raw table with ALL items from ALL target flyers, including electronics, home goods, and items with missing fields. Cleaning happens in Stage 3, not here.

### 4.1 Completeness check — loud warning, never silent

After extraction, the tool checks: expected stores vs. stores actually extracted.

- If a store is missing, the tool raises a **LOUD WARNING naming the specific store(s)**.
- Two different cases, and the warning must say which one it is:
- **Case A — store genuinely has no active flyer.** This happens. It is OK. The tool **warns and continues** with the remaining stores.
- **Case B — extraction failed, or the store was missed by mistake.** This is the real error. The tool **warns and continues** — it completes the run with the remaining stores, but the warning must state clearly that this store's data is missing due to extraction failure (not because the store has no flyer), so Ellie knows the comparison is incomplete for that store.
- The tool NEVER silently produces a file from incomplete data without warning. A missing flyer that nobody notices is how the "lowest price" becomes wrong. In both cases the rule is the same: **warn and continue.**

---

## 5. Stage 3 — ETL (Extract, Transform, Load)

This is the preparation stage. Goal: turn the raw table into ONE clean table that contains **only grocery items in our categories, with complete and normalized information.**

### 5.1 Category filter — drop what we never match

The target list is grocery only: vegetables, legumes, dairy, meat, pantry, etc.

- Remove items in categories outside the list: electronics, home electronics, household non-food, clothing, etc.
- This is done FIRST on complete items, because it sharply reduces the data volume — fewer items to clean, fewer items to match, faster tool.
- **Do NOT trust Raddar's category labels blindly.** They are inconsistent across retailers. A category mapping per retailer is used as a helper, but it is not the final word.

### 5.2 Split: complete vs. incomplete items

After extraction, split the table in two:

- **Complete items:** have title, price, and image.
- **Incomplete items:** missing one or more of title, price, image, or category.

**Why split:** the category of an incomplete item cannot be trusted (an item with no title may be miscategorized). Filtering and filling are handled differently for each group.

### 5.3 Clean the complete items

For complete items, in order:

1. **Deduplicate** — after normalization, same store + same normalized title + same price = one row. Near-duplicates with slightly different titles ("Butter 454g" vs "Butter, 454 g") are caught because dedupe runs AFTER normalization, not before.
2. **Normalize** — make all values comparable (see 5.7).
3. **Category re-check** — drop any item whose category is outside the target list.

### 5.4 Fill the incomplete items (Gemini 2.5 Flash)

For incomplete items, use Gemini 2.5 Flash with the item image:

- **Missing title:** Gemini reads the image and writes a title/name. If the image contains text (brand, weight, pack size), Gemini adds that text to the description.
- **Missing category:** Gemini reads the image and assigns a category. An item is NEVER dropped just because its category is unknown, blank, or unclassified — the image assigns it.
- **Missing price:** the item cannot be used for price comparison. It is dropped after the fill attempt (a match without a price is useless).
- **Missing image:** the item is NOT dropped. Matching continues on title, description, price, and other information. The image is only needed for Ellie's visual verification, not for matching.

**Why the LLM here and not a person:** hundreds of incomplete items per week. Manual filling is slow and inconsistent. Gemini reads images fast, and this is the one place where image understanding is genuinely needed.

### 5.5 Re-check the filled items

After filling, each previously-incomplete item gets its category checked again against the target list. Items outside the list are dropped.

### 5.6 Merge into one clean table

Merge the cleaned complete items + the filled-and-checked incomplete items into ONE table. Both groups share the same columns, the same normalization, and the same formatting.

### 5.7 Normalization — structured fields, not just cleaned text

"454 g = 1 lb" is not enough. Every item is parsed into structured fields:

| Field | Meaning | Example |
| --- | --- | --- |
| brand | Brand name | No Name, Selection, Compliments |
| product\_type | What the product is | butter, milk, strawberries |
| flavor\_variant | Flavor / variant | salted, unsalted, 2% |
| pack\_count | Number of units in the pack | 4 (in "4 x 100 g") |
| unit\_size | Size of one unit | 100 g |
| unit\_type | Unit of measure | g, kg, lb, oz, mL, L, count |
| total\_size | pack\_count x unit\_size | 400 g |
| language | Language of the title | en / fr |
| price | Price as a number | 3.99 |
| price\_per\_unit | Price per standard unit | per 100 g |

**Why structured fields:** the multipack rule (4 x 100 g cups is NOT equal to one 400 g tub) can only work if the tool knows the pack structure — not just the total weight. "2 x 200 g" must parse into pack=2, unit=200 g, total=400 g.

**Other normalization rules:**

- Units converted to standard units: g, kg, lb, oz, mL, L. 454 g = 1 lb.
- Numbers in one format: no currency symbols, no commas inside numbers, one decimal style.
- Titles in consistent case (title case).
- Accents stripped for matching ("legumes" matches "legumes" with accent).

**Brand mapping / private-label equivalence:** Food Basics **Selection** = Loblaw **No Name**. Selection butter matches No Name butter as EQUAL.

### 5.8 The rule-learning loop — how normalization gets smarter

New situations WILL appear that no rule covers yet. The answer is not to list every possible situation in advance (impossible). The answer is a loop:

1. Ellie reviews the output and marks a row incorrect, with a reason: "this is wrong because...".
2. That feedback is entered as a correction (an input text / audit note).
3. The correction is translated into a new normalization rule and added to the rules table.
4. The Gemini prompt includes the updated rules table, so the next run applies the new rule.

**Example:** Ellie marks a match wrong because "2 x 200 g" was treated as "400 g single". A new rule is added: "IF title contains a pack pattern (N x SIZE), THEN set pack\_count = N and never treat it as a single unit." Next week, Gemini applies it.

The rules table is a living document. Every audit makes it stronger.

### 5.9 Output of Stage 3

ONE clean table: every row is a grocery item in our categories, with structured fields, title, price, image (or "no image"), page, validity dates, store, category — all normalized, deduplicated, and complete.

**No image-based matching happens after this stage.** Image understanding is finished here.

---

## 6. Stage 4 — Matching

**DECIDED STRATEGY: B (Idea 2) — Rules first, Gemini judges, confidence-triggered retry.**

```
Layer 1 — RULES:      generous candidate generation (spelling)
Layer 2 — GEMINI:     judgment (match + confidence)
Layer 3 — RETRY:      widen rules + embedding similarity (meaning) — only when confidence is low
Layer 4 — HUMAN:      review queue — only when still low
```

Rules never make the final decision — they only produce a short list of candidates. Gemini makes the judgment call. This prevents rules and the LLM from contradicting each other.

**Idea 1 REJECTED:** pre-splitting items into "easy" (rules) and "hard" (Gemini) BEFORE matching does NOT work. Difficulty is only known AFTER matching — to predict it in advance you would need a system that judges the data, which is itself a matching system. Chicken and egg. Rejected.

### 6.1 Input

- The clean flyer table (Stage 3 output).
- The target item list: fixed, but editable (items can be added or changed). Each list item has a name and a target quantity/size.

### 6.2 Layer 1 — Rule-based candidate generation (generous)

For each list item, the rules produce up to **20–25 candidates** from the clean table, using MULTIPLE signals (any one can qualify a candidate):

1. **Token overlap** — candidate title shares meaningful words with the list item name (brand + product type). Bare numbers ("1", "2") are ignored as tokens.
2. **Brand match** — candidate brand matches the list item brand (or its private-label equivalent).
3. **Category + price band** — candidate is in the same broad category AND its price is in a plausible band for the item.

**Quantity compatibility** — candidate size must be compatible with the list item size, within tolerance:

| Case | Tolerance (starting default) |
| --- | --- |
| Weight (g / kg / lb) | +/-2% or +/-5 units, whichever is larger. Example: 399 g \~ 400 g. |
| Volume (mL / L) | +/-2% or +/-5 units, whichever is larger. Example: 680 mL \~ 700 mL. |
| Count / pack | Must match exactly (a 4-pack is not a 6-pack). |
| Multipack vs. single | A multipack is NOT equal to a single unit (4 x 100 g cups is not one 400 g tub) unless the list item explicitly allows it. |

**Tolerance note:** +/-2% is the starting default. Per-category tuning comes later, only when the audit shows a category failing. Do not over-engineer now.

**Why rules here, not the LLM:** Layer 1 is a coverage problem (don't miss the true match). Coverage is cheap with rules. Layer 2 is a judgment problem (is this the same product?). Judgment is where the LLM earns its cost. Using the LLM twice (filter + judge) doubles cost for almost no gain.

### 6.3 OR-items — both directions

**Direction 1 (list side):** if the list item says "Item A OR Item B OR Item C", it is expanded into three sub-queries. Matching ANY ONE of them counts as a match. The tool does NOT need to find all three.

**Direction 2 (flyer side):** if a competitor's flyer item says "A or B or D" and the list is searching for B, the competitor's multi-item offer matches B.

**In both directions, the output must show WHICH item matched** (e.g. "matched: B"), so Ellie can verify it.

### 6.4 Layer 2 — Gemini judgment (Gemini 2.5 Flash)

For each list item, Gemini receives: the list item + the candidate list (from Layer 1). It returns:

```json
{
  "match": "EXACT | STRONG | RELATED | NO MATCH",
  "offer_index": <index of best candidate, or null>,
  "reason": "<one line>",
  "confidence": <0.0 - 1.0>
}
```

**Match classes:**

| Class | Meaning | Used as the price? |
| --- | --- | --- |
| EXACT | Same product, same brand (or private-label equivalent), same size within tolerance | Yes — primary price |
| STRONG | Very close, but small difference (e.g. size slightly off, near-equivalent brand) | Yes, but flagged for human review |
| RELATED | Similar product, not the same (collision / near-match) | No — kept for reference only |
| NO MATCH | Nothing comparable | No — output shows "not found" |

**Private-label rule (from Ellie):** Food Basics **Selection** = No Name equivalent. Selection butter matches No Name butter as **EXACT**.

**If Gemini is unsure, it downgrades** (EXACT -> STRONG, STRONG -> RELATED). A wrong EXACT is worse than a cautious STRONG.

**The confidence score is not decoration — it drives Layer 3.** Low confidence triggers the retry. See 6.5.

### 6.5 Layer 3 — Confidence-triggered retry (widen + embeddings)

**Trigger:** Gemini's confidence is below the threshold, OR Layer 1 returned fewer than 3 candidates, OR the result was NO MATCH.

**Two actions, both run on retry:**

**(a) Widen the rules.** The filters relax automatically: wider tolerance, category gate dropped. New candidates are added to the pool.

**(b) Embedding similarity search.** Every title is converted to a vector (an embedding) that represents its MEANING. The tool finds flyer items whose vectors are closest to the list item's vector — even if they share zero words.

| Signal | What it catches | Example |
| --- | --- | --- |
| Rules (spelling) | Same words | "butter 454g" \~ "butter, 454 g" |
| Embeddings (meaning) | Same idea, different words | "soda" \~ "pop"; "beurre" \~ "butter"; "sparkling water" \~ "carbonated water" |

**Why this fixes the missed-candidate fear:** rules catch spelling, embeddings catch meaning. A true match missed by spelling is caught by meaning. After the retry, Gemini judges the expanded candidate set again.

**Cost control:** embeddings run ONLY on retry (the hard minority), not on every item. The common case stays fast and cheap.

### 6.6 Lowest-price selection

- Among EXACT matches for a list item, pick the **lowest price**. If two EXACTs tie, prefer the one with the highest confidence.
- If there is no EXACT, take the best STRONG and **flag it for review**.
- If there is no EXACT and no STRONG, the item is **not found**.

### 6.7 Superstore comparison

The same matching logic runs a second time, restricted to Superstore flyer offers only. This gives the Superstore price for the same item, side by side with the competitor price.

- If Superstore has no match, the Superstore columns are left empty / "not found". This is normal — not an error.

### 6.8 STRONG matches — recorded, never promoted

When Ellie reviews a STRONG match and confirms it is strong, that pair is saved as a **STRONG record**. This means:

- The pair is known: "this item strongly matches this list item."
- Next week, the tool does NOT auto-pick it, does NOT favor it, and does NOT promote it to EXACT.
- The stored record only tells the reviewer: "we have seen this pair before, Ellie already confirmed it is strong" — so she does not re-review the same pair every week.
- The tool still runs the full matching from scratch every week.

**Why:** a STRONG match is strong for this week's flyer and price. Next week's flyer may have a better EXACT. Auto-picking last week's STRONG would block a better match.

### 6.9 Why this design — Strategy B, and the alternatives

| Strategy | Easiest | Fastest / cheapest | Most accurate |
| --- | --- | --- | --- |
| A: LLM matches first | Best — one system | Worst — LLM reads thousands of items | Good — but long-context degradation + hallucination |
| **B: Rules + LLM fallback (CHOSEN)** | Medium | Best — LLM only on candidates + retries | Good — \~95% of C's accuracy |
| C: Both in parallel + compare | Worst — two systems + arbiter | Worst — pays for both | Best in theory — but only \~2-4% better than B |

**B was chosen** because it is the fastest and cheapest, and gets \~95% of C's accuracy. **C stays in reserve:** if the golden test shows accuracy below target, upgrade ONLY the hard items to C (run both systems on just the items that failed), not everything.

**Four nets:** rules (spelling) + embeddings (meaning) + Gemini (judgment) + human review (safety). A true match would have to slip through all four to be lost.

---

## 7. Stage 5 — Output (Excel)

**Rule: the file contains ALL list items (69-70). Nothing is hidden. The ORDER does the prioritizing.**

One workbook, four sheets.

### 7.1 Sheet 1 — Summary (Ellie lands here)

Counts in the same order as the Action List, plus the top 10 biggest price gaps ranked:

- Items checked
- Group 1 — Superstore HIGHER: count
- Group 2 — Not in Superstore: count
- Group 3 — Only in Superstore: count
- Group 4 — Not found anywhere: count
- Group 5 — Equal / OK: count
- Needs review: count

### 7.2 Sheet 2 — Action List (the full list, in priority order)

One row per list item. Columns, in this order:

| Column | Content |
| --- | --- |
| Action | The verdict: SUPERSTORE HIGHER / NOT IN SUPERSTORE / ONLY IN SUPERSTORE / NOT FOUND / EQUAL |
| List item | Name from the target list |
| Price gap $ | Superstore price - competitor price (e.g. +$1.50) |
| Price gap % | Gap as % of competitor price (e.g. +33%) |
| Competitor store | Store with the lowest matching price |
| Competitor item + image | Title + embedded thumbnail |
| Competitor price / page / validity | Price, flyer page, validity dates |
| Superstore item + image | Title + embedded thumbnail |
| Superstore price / page / validity | Price, flyer page, validity dates |
| Match class | EXACT / STRONG / RELATED / NO MATCH |
| Why this match | Gemini's one-line reason |
| Flags | Review needed? Based on image? |
| Ellie's decision | Empty column for her: Approve / Reject / Change |

**Sort order — the 5 groups:**

| Group | Meaning | Sort inside group |
| --- | --- | --- |
| 1. SUPERSTORE HIGHER | Superstore price > lowest competitor price | Gap % descending — biggest overcharge on top |
| 2. NOT IN SUPERSTORE | Competitor has it, Superstore flyer does not | Competitor price ascending (cheapest first) |
| 3. ONLY IN SUPERSTORE | Superstore has it, no competitor flyer has it | Alphabetical |
| 4. NOT FOUND ANYWHERE | Neither Superstore nor any competitor has it | Alphabetical |
| 5. EQUAL / OK | Both have it at the same price, or Superstore is cheaper | Alphabetical |

**Why this order:** Ellie checks group 1 first (price changes = the money), then group 2 (gaps in our flyer). Groups 3-5 are reference — she can stop whenever she's done. Items where Superstore is CHEAPER than competitors sit in group 5 — also nothing to do.

**Colors:**

- Group 1 rows: light red — act
- Group 2 rows: amber/orange — check
- Groups 3-5 rows: grey — nothing to do
- Review flag: yellow badge on the row

**Images:** embedded thumbnails (\~100 px) next to each item name — competitor image beside Superstore image, so Ellie verifies sameness in 2 seconds without clicking.

**Rules for the output:**

- If no competitor match: competitor columns show "not found".
- If no Superstore match: Superstore columns show "not found" — the competitor result still stands.
- Images embedded so Ellie can verify without opening links. Page numbers give traceability.
- Validity dates included because a price comparison is only fair if the validity windows overlap.

### 7.3 Sheet 3 — Review Queue

Only the flagged rows (STRONG + low confidence after retry), same columns as the Action List. Ellie clears this queue and she's done.

### 7.4 Sheet 4 — Warnings

- Stores with missing flyers (with which case: no flyer vs. extraction failed)
- Carried-over 2-week flyers (marked CHECK WITH ELLIE)
- Data notes (e.g. "12 items had info from image analysis")

## 8. Stage 6 — Learning loop (after output)

**Purpose:** the tool gets smarter every week. Ellie's corrections from the Review Queue and the Action List become new rules that next week's run uses.

**Why it is NOT inside the matching layers:** Layers 1-4 are the weekly RUN — they execute. Learning happens AFTER the output, from human corrections, and feeds NEXT week's run. It never changes the current run's results.

**The loop, step by step:**

1. **Ellie reviews** — fills Approve / Reject / Change + a reason ("this is wrong because...") in the Review Queue and Action List decision columns.
2. **Corrections are collected** — after the run, the tool reads Ellie's decisions + reasons.
3. **Corrections go to memory** — three kinds of memory:

- **Normalization rules** — e.g. "pack pattern N x SIZE = pack, never single."
- **Matching rules** — e.g. "Brand A = Brand B as equivalent."
- **Confirmed STRONG pair records** — pair seen + confirmed: don't re-review, never auto-promote to EXACT.

4. **Memory becomes rules** — each correction is translated into a rule and added to the rules table. The rules table is a living document.
5. **Next week's run uses the new rules** — Gemini's prompt includes the updated rules table; the rule engine uses the updated rules.
6. **Golden test re-measures** — the tool re-runs the 50-item golden test set to confirm accuracy actually improved.

**Guardrail — test before live:** a new rule goes live ONLY after it passes the golden test set. A rule that fixes one item but breaks five others is worse than no rule. If a new rule lowers golden-test accuracy, it is rejected, and the correction stays as a one-off note, not a rule.

**What is stored, what is not:**

- Stored: the rule + the reason + the example that created it + the date.
- Not stored: the full weekly output (too big, not needed). The rules table + STRONG records ARE the memory.

**The result over time:** first run \~85-90% correct → after 2-3 weeks of feedback \~92-96% → under 8% corrections by week 3. The loop is what makes those numbers possible.

## 9. Quality system — how we know it works

### 9.1 Golden test set

50 list items, hand-matched by Mohamed against real flyers. Run the tool, measure % correct. This is the only honest way to claim accuracy.

The golden test measures two numbers:

- **Precision:** of the matches the tool claims, how many are right?
- **Recall:** of the true matches that exist, how many did Layer 1 include? (This is the number that answers the missed-candidate fear.)

### 9.2 Weekly accuracy tracking

Every week, track: **% of output rows Ellie has to correct.** This is the one number that matters.

**Targets:**

- First run: \~85-90% of rows fully correct.
- After 2-3 weeks of feedback loop: \~92-96%.
- **Target: under 8% of rows needing correction by week 3.**

### 9.3 What counts as "correct"

- An EXACT that is truly exact: correct.
- A STRONG correctly flagged for review: the system working as designed, not a failure.
- A wrong EXACT, a missed true match, or a STRONG that should have been EXACT: errors to fix.

### 9.4 Why 100% is not the target

Some flyer data is genuinely ambiguous (bad image, salted vs. unsalted not visible, unclear pack size). No system gets those right every time. The goal is: high accuracy on clear cases, honest flags on unclear ones, and a loop that keeps improving.

---

## 10. Design decisions — summary

| Decision | Why |
| --- | --- |
| Only this-week flyers (2-week carry-over kept, marked CHECK WITH ELLIE) | Avoids stale comparisons; keeps live prices. |
| Completeness check: warn and continue (both cases) | A missing flyer nobody notices = a wrong "lowest price". Warn, name the store, say which case. |
| Category filter before cleaning | Drops most of the data early — the tool is faster and cheaper. |
| Never drop for missing category — Gemini assigns it from image | Raddar categories are unreliable; dropping on bad data loses real items. |
| Never drop no-image items | Matching needs title + price, not the image. |
| Split complete / incomplete | Incomplete items have unreliable categories — they must not be filtered on bad data. |
| LLM fills missing info from images | Fast, consistent, and the only step that truly needs image understanding. |
| Structured quantity fields | Pack structure (pack\_count, unit\_size, total\_size) is required for the multipack rule. |
| Rule-learning loop from Ellie's feedback | New situations will always appear. The loop captures them as rules instead of pretending the list is complete. |
| Stage 6 learning loop (after output) | Learning sits AFTER the output, not inside the matching layers. Corrections -> memory -> rules for next week's run. |
| Golden-test guardrail on new rules | A new rule goes live only if it passes the golden test set. A rule that fixes one item but breaks five is rejected. |
| Strategy B: rules narrow (generous), Gemini decides, confidence retry | Fastest + cheapest; \~95% of C's accuracy. |
| Embedding safety net on retry only | Rules catch spelling, embeddings catch meaning (soda/pop, beurre/butter). Runs only on retry — keeps it fast and cheap. |
| Idea 1 (pre-split easy/hard) REJECTED | Chicken-and-egg: difficulty is only known after matching. |
| Lowest price among EXACTs | The business question is: "what is the cheapest comparable price?" |
| STRONG recorded, never promoted | A STRONG is strong for this week. Auto-promoting it would block a better EXACT next week. |
| +/-2% tolerance as starting default | Simple, reasonable. Tune per category only when audit data shows a category failing. |
| Validity dates + pages in output | Price comparisons need overlapping windows; pages give traceability. |
| Output: full list, 5-group priority sort, 4 sheets | Ellie sees all 69-70 items, but the two action groups come first. Summary mirrors the same order. Gap $ and % precomputed; images embedded; decision column for Ellie. |
| "Based on image" flag | Image-guessed info is weaker. The reader must know. |

---

## 11. Open questions (to confirm with Ellie)

1. **The 2-week carry-over case:** keep flyers that started last week but are valid all this week? (Recommendation in this document: yes, marked CHECK WITH ELLIE.)
2. **STRONG threshold:** is flagging \~10% of rows for review acceptable, or should the bar be tighter?
3. Exact region definition for the flyer filter.
4. Confirm the final store list (9 banners) — add or remove any?
5. Where should the weekly output Excel be saved (shared folder / SharePoint)?
6. Tolerance: is +/-2% the right starting default, or should any category start wider?
7. Which embedding model for the Layer 3 retry (Gemini embedding API vs. a local model)?

---

*End of document — v5.0 FINAL.*
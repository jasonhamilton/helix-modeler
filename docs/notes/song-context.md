# Song and artist context as a prior for the starting tone ([T104](../../board/TASKS.md#t104 "Song and artist context as a prior"))

Design report, 2026-10-17, owner: research. Status: proposal, nothing built into the pipeline.
Not legal advice. No scraped data, no copied text; sources are linked. Prototype:
`scripts/song_meta_probe.py` (dry-run default, never run against the network by the author, see 1.4).

Idea: artist + title -> genre / era / (maybe) rig facts -> prior over chain structure and models ->
seeds for search. Honest summary first: **metadata is a weak prior, rig facts are a weaker and
legally harder one, and the audio-based detector ([T102](../../board/TASKS.md#t102 "Block-presence detector for the chain builder")) will beat both whenever audio exists.**
Build the cheap, compliant half (genre + era tags -> category-level prior) and measure it before
doing anything else.

## 1. Song metadata sources

Only the **artist and title strings** leave the machine. Never audio, file names, paths, stem hashes
or the user's presets.

### 1.1 Comparison

| Source | Licence of data | Terms / key / rate | Guitar-music coverage | Verdict |
|---|---|---|---|---|
| [MusicBrainz](https://musicbrainz.org/doc/MusicBrainz_API) | Core data (artists, recordings, releases, labels) CC0; **tags and genres are supplementary, CC BY-NC-SA 3.0** ([data licence](https://musicbrainz.org/doc/About/Data_License), [database page](https://musicbrainz.org/doc/MusicBrainz_Database)) | Free for non-commercial use, no key, meaningful User-Agent with contact required, 1 request/s average, 503 and blocking if abused ([rate limiting](https://musicbrainz.org/doc/MusicBrainz_API/Rate_Limiting)) | Very good for year, label, release; tags/genres are crowd-sourced and sparse on obscure tracks, good on famous rock | **Primary** |
| [Wikidata](https://www.wikidata.org/wiki/Wikidata:Licensing) | All structured data CC0 | No key; Wikimedia User-Agent policy ([policy](https://foundation.wikimedia.org/wiki/Policy:Wikimedia_Foundation_User-Agent_Policy)); numeric limits not confirmed here, back off on 429 ([data access](https://www.wikidata.org/wiki/Wikidata:Data_access)) | Good for famous artists (genre P136, inception, country); recordings/songs patchy | **Fallback** (artist-level genre and era) |
| [Last.fm API](https://www.last.fm/api/tos) | Tags are user data; ToS: non-commercial by default, 100 MB storage cap, attribution, cannot sub-license | Key required (`api_key`; [tag.getTopTags](https://www.last.fm/api/show/tag.getTopTags) shows the pattern); rate caps unstated here; public pages need Last.fm's prior approval | Best folksonomy for rock sub-genres | Optional enrichment, user supplies own key |
| [Discogs API](https://support.discogs.com/hc/en-us/articles/360009334593) | Mix of CC0 data and "Restricted Data" per its API terms (terms page returned 403 to my fetcher, so details not read) | Registered app, unique User-Agent, 60 req/min authenticated, 25 unauthenticated per search result summaries; terms not verified | Strong for label, year, style (e.g. "Hard Rock"), pressing-level noise | Do not use until the terms are read by a human |
| [Spotify Web API](https://developer.spotify.com/terms) | Spotify Content | Terms **forbid using Spotify content to train an ML/AI model** and limit caching. Since 2024-11-27 new apps lose audio-features, recommendations, related artists ([blog](https://developer.spotify.com/blog/2024-11-27-changes-to-the-web-api)); Feb 2026 dev-mode requires owner Premium, 5 users, fewer endpoints ([migration guide](https://developer.spotify.com/documentation/web-api/tutorials/february-2026-migration-guide)) | Good popularity/genre but gated | **Reject**: OAuth app, Premium, ML clause, moving target |
| [iTunes Search API](https://developer.apple.com/library/archive/documentation/AudioVideo/Conceptual/iTuneSearchAPI/index.html) | Apple content; promotional-use conditions | No key; limits and field list not confirmed here (I believe `primaryGenreName`, `releaseDate`; verify) | Only coarse store genres ("Rock") | Not recommended: coarse, terms oriented to promotion |
| AcousticBrainz, Deezer, TheAudioDB, Genius | not checked | not checked | AcousticBrainz is audio-feature data and was discontinued (not re-verified); Genius is lyrics | Skip |

Why not just the tag counts everywhere: genre labels are coarse ("rock") and only weakly tied to
gain level or ambience; year is probably the more useful field (era -> amp family, gain level,
reverb/delay habits).

### 1.2 Recommendation

- **Primary: MusicBrainz.** CC0 core data gives year, label, artist identity; user's local use of the
  web service is non-commercial and free; no key. Genre tags are CC BY-NC-SA: use them locally,
  cache them locally, **never commit them, never ship them in a data file or a trained artefact**
  (a model trained on them is arguably an adaptation; avoid by using tags only as live inputs and
  by deriving only aggregate statistics from the user's own corpus). If the project is ever sold or
  hosted as a service, MusicBrainz needs a commercial plan.
- **Fallback: Wikidata** (CC0, no ND/NC question) for artist genre and active years when MusicBrainz
  has no hit or no tags. Optional extra: Last.fm with the user's own key.
- **Offline option (needs no network at all):** MusicBrainz publishes full database dumps; a user
  can download them once and query locally. Large; mention only, not planned.

### 1.3 Sample queries

MusicBrainz recording search (core + tags in one call; `inc=genres` works on lookups, search results
carry `tags`):

```
GET https://musicbrainz.org/ws/2/recording?query=recording:"Enter Sandman" AND artist:"Metallica"&fmt=json&limit=5
User-Agent: tonematch-song-probe/0.1 ( you@example.org )
```

Genres for an artist/release-group by id: `.../ws/2/release-group/<mbid>?inc=genres&fmt=json`
([API doc](https://musicbrainz.org/doc/MusicBrainz_API)). Label needs a release lookup with
`inc=labels`; the probe does not do it.

Wikidata (SPARQL, CC0) for artist genre, illustrative: `SELECT ?g WHERE { ?a rdfs:label "Metallica"@en;
wdt:P136 ?g }` against `https://query.wikidata.org/sparql` with a Wikimedia-style User-Agent.

### 1.4 Prototype and terms check

`scripts/song_meta_probe.py` queries MusicBrainz only, 3 built-in example songs, parses year and top
tags with a pure function, caches under `cache/song_meta/` (gitignored). `--dry-run` is the default
and prints the URLs. A live run needs `--live --accept-privacy` and a contact in `TONEMATCH_CONTACT`,
runs at 1 request per 1.1 s. **Terms checked 2026-10-17** (pages above: non-commercial use free, no
key, 1 req/s, descriptive User-Agent): a live run is permitted. I did **not** run it, and in this
session I could not run any shell command, so `tests/test_song_meta_probe.py` (5 offline tests) and
the dry run are written but unexecuted. Run `python -m pytest tests/test_song_meta_probe.py` first.

### 1.5 Privacy: how the user is told and opts in

1. Feature off by default. Config key `song_context.enabled = false`; CLI `--song-context` (or the
   GUI checkbox) turns it on for one run, `--remember-song-context` for good.
2. First use prints (and requires typing `y` to) this notice, exact strings shown:
   "Sending ARTIST='Metallica' TITLE='Enter Sandman' to musicbrainz.org (and wikidata.org as
   fallback). Nothing else leaves your machine: no audio, no file names, no presets. They see your
   IP address and our User-Agent. Results are cached in cache/song_meta/."
3. The user may type artist/title by hand; the tool never reads them from file tags unless
   `--from-tags` is passed, and still shows the strings before sending.
4. A `--offline` switch and "no network" environment variable make every function raise before any
   socket opens; unit tests assert this.
5. Each request logged locally (URL, time) in `cache/song_meta/requests.log`, so the user can audit.
6. Mark in the ledger that a run used song context (a field in the match report, not a ledger kind).

## 2. Artist rig and pedalboard sources

Strict rule: only official APIs, manual lookup, or links/text the user supplies. **No scraping of any
site below.** I could not retrieve several terms pages (404/403) and treat "unverified" as "manual
only".

| Source | Contains | Reliability | Terms / access |
|---|---|---|---|
| [Equipboard](https://equipboard.com/) | Per-artist gear lists (amps, pedals), user-submitted with sources, sometimes per tour | Mixed: crowdsourced, often stale, rarely settings or order | `robots.txt` ([link](https://equipboard.com/robots.txt)) blocks member gear lists, search, filters and 600+ named bots; I could not retrieve its terms page (404), no public API found. **Manual lookup only** |
| Premier Guitar "Rig Rundown" ([site](https://www.premierguitar.com/)) | Video + article: amps, pedals, guitars, often signal order and some settings, for touring players | High for the tour it covers; time-bound | [Terms](https://www.premierguitar.com/terms-of-use) reserve copyright and bar copying/republishing; no API. **Read it, note facts by hand, link back** |
| Guitar World / Guitar.com / other Future plc and magazine gear pieces | Interviews, "gear" features; amps and pedals, occasionally settings | Medium to high; quoting artists | Future's site terms pages I tried returned 404, so unverified; no API. **Manual, facts only, link** |
| Artist interviews (YouTube, podcasts, magazines) | Anything, including settings | Varies; the artist's own words are best | Copyright applies to the text; user-provided link or typed facts only |
| [Wikipedia](https://en.wikipedia.org/) / Wikidata | Equipment sections on some artist and album articles; Wikidata has little gear | Medium; cited sentences | CC BY-SA 4.0 text, official [API](https://www.mediawiki.org/wiki/API:Main_page) with User-Agent policy; **compliant for automated access**, but text is share-alike, so use as a lookup and store only your own extracted facts with a link, no committed text. Gear sections are rare |
| Ground Guitar ([groundguitar.com](https://groundguitar.com/)) | Pedalboard pictures and lists (user/artist boards) | Medium | Terms not retrieved. Manual only |
| Gearspace / forum threads, manufacturer artist pages, YouTube gear demos | Anecdotal / marketing | Low to medium | Forum ToS typically restrict automation; manual only |
| Album liner notes and credits | Amp/guitar credits on some records | High when present | Facts only; MusicBrainz relationship data (CC0) sometimes lists instruments, not gear models |

Compliant access summary:

1. **Manual lookup is the only path** for the rig sites. The tool can open a search URL in the
   user's browser ("Look this up: Equipboard / Rig Rundown for <artist>") and accept what the user
   types or pastes.
2. **User-provided links:** the user pastes an article URL; the tool does not fetch it. It shows a
   short form ("amp:", "drive:", "delay:", "reverb:", "notes:") the user fills in by reading.
   Optional later: fetch only the single page the user pasted, only if the site's terms and
   robots.txt allow it, with the user's confirmation; default is no fetch.
3. **A small hand-curated local file** (`~/.tonematch/rigs.json`, user-owned) of facts such as
   `{"artist": "...", "amp": "Marshall JCM800", "drive": ["Ibanez TS808"], "source_url": "..."}`.
   Facts are not copyrightable, but selection and wording of a database can be; keep it personal
   and not committed. No aggregated copies of a site's lists.
4. Treat rig facts as **artist- or tour-specific**, not song-specific. Studio tone of one song often
   differs from the touring board; mark confidence accordingly.

## 3. Mapping real gear to Helix models

Line 6 names many models after the gear they emulate and documents the "based on" names in its
manuals and in community lists (index: HelixHelp model list; note the PLAN link
`helixhelp.netlify.com/models/` returned 404 on 2026-10-17, so the index entry needs a replacement
link, which the lead should decide). Example pairing (public, widely stated): Helix "Scream 808" is
modelled on the Ibanez TS808
([Line 6 forum thread](https://line6.com/support/topic/53613-how-close-does-it-sound-ibanez-ts808-tube-screamer-line-6-scream-808)).
Treat all pairings as illustrative until checked against the user's own catalog.

**What is Line 6 content and must stay local:** the catalog (model ids, display names, parameter
lists) from the user's HX Edit install, and the "based on" strings from manuals or the model list
copied out of them. Both live in `cache/` (gitignored), exactly like the existing catalog cache.

**Proposed design**

1. `real_gear_aliases.json` (committed, **no Line 6 text**): keys are normalised real-world gear
   names with a category, e.g. `{"ts808": {"category": "drive", "aliases": ["ibanez ts808",
   "tube screamer 808", "ts-808"]}}`. Real gear names are facts, not Line 6 assets. No Helix model
   ids or model names in this file.
2. Local `cache/gear_map/helix_names.json` built at runtime from the user's catalog: model id,
   display name, category (via `chain_prior.categorize`). The "based on" text, if the user supplies
   the manual/list as a local file, is added as a second, local-only column. We never download it
   into the repo.
3. Local `cache/gear_map/overrides.json`: the user's or maintainer's curated real-gear -> model-id
   pairs. Since a pair contains a Helix id (Line 6 identifier), keep it local; share only as
   instructions ("name ->  category") if at all.
4. Resolution at query time: normalise (lowercase, strip brand punctuation), exact alias hit,
   then token-set fuzzy match against the catalog display names and based-on strings, accept above a
   threshold, and otherwise **fall back to category only** ("an overdrive pedal") so the prior still
   gets a category signal. Return `(model_id | None, category, confidence)`.
5. Amps and cabs: an amp is a family (Plexi, JCM800, Twin Reverb); map to the Helix amp, then pick
   the cab from the same record when known, else leave the cab to search.
6. Unit-test with a fake mini catalog (no Line 6 data in the test).

## 4. Predictive pipeline

```
artist + title (typed, opt-in)  ->  metadata fetch (1)  ->  features: era bucket, genre tags (coarse
families), optional user rig facts  ->  prior over chain  ->  search seeds
```

### 4.1 Prior

- Features: `era` (decade), `genre family` (a small fixed list mapped from raw tags: blues, classic
  rock, hard rock, metal, punk/indie, pop, country, jazz, funk/soul, ...), optional `rig` facts.
- Corpus: the 126 setlist presets + goldens in `data/chain_prior/category_prior.json`'s source
  ([T101](../../board/TASKS.md#t101 "Chain-pattern model for the chain builder")) are the human's own, grouped by artist/decade/style by the preset agent. 128 usable
  presets is small, so condition with **shrinkage**: P(category present | genre) =
  (n_g * p_g + k * p_all) / (n_g + k), k about 10-20. Never let a group of 3 presets set a
  probability to 0 or 1.
- Output: a vector of category presence probabilities plus an adjusted bigram table, in the same shape
  `chain_prior` already uses. Model-level prior inside a category (which drive, which amp): from the
  local `cache/chain-patterns/model_frequencies.json` with the same shrinkage, then re-weighted by
  mapped rig facts (a stated JCM800 raises the matching Helix amp strongly, otherwise nothing).

### 4.2 Where it plugs in

- **`src/tonematch/chain_prior.py` (preset agent, not edited here):** add an optional
  `context: SongContext | None` argument to the query API (`sequence_logprob`, `top_structures`) that
  selects a conditional prior; context `None` returns today's behaviour, so nothing breaks. The
  committed `category_prior.json` has category-level data only, so the conditional tables (by genre
  and era) must follow the same rule: aggregate counts only, no preset names. Needs the preset agent
  to say how many presets fall in each group before any group is used.
- **Search seeds (search agent):** `search-design.md` lists "predictor and retrieval seeding" as not
  built (`match` takes a candidate list). The song prior supplies (a) the ordered category structure
  candidates from `top_structures(context=...)`, ranked below acoustic evidence, and (b) per-category
  model candidates ordered by prior. It should be one input to candidate **ordering and Gaussian
  prior weight**, never a hard filter. T050/T102 audio evidence overrides: if the presence detector
  says there is no delay, the song prior cannot add one.
- The metadata fetch itself is a small module outside the search loop (`tonematch.song_context`
  proposed), called by `match_song.py` only when the opt-in flag is set.

### 4.3 Value, honestly

- The prior can only choose among the plausible chains the corpus already contains. The corpus
  statistics show most chains share one skeleton (drive > amp > cab > mod > delay > reverb,
  chain-patterns note): the unconditional prior already captures much of the structure, so genre
  adds **little on structure** and mostly shifts presence of mod, delay, comp and gain level.
- The audio side (separated stem, [T102](../../board/TASKS.md#t102 "Block-presence detector for the chain builder") detector) measures what is actually on the recording; a
  genre guess is weaker than ears on audio. Studio production, doubling and mixing also decouple
  "genre" from "this tone".
- With 128 presets from one player's library, "artist" groups are nearly all n < 5: artist
  conditioning is **not estimable**; decade and genre-family maybe. Expect the gain, if any, in:
  (a) the first search seed (fewer wasted evaluations), (b) tie-breaking between near-equal models,
  (c) the no-DI path when audio evidence is weak.
- Rig facts (e.g. "plays JCM800 + TS808") are potentially the strongest single input for model
  choice, but need the user to supply them and Helix models are not 1:1 with real gear. Probably
  helps a lot for a minority of songs.
- Rough guess, flagged as a guess: a small improvement in held-out log-likelihood of chain category
  presence (single-digit percent), larger only for gain level and era-typical ambience. It could be
  zero. Cost is real: a network dependency, a privacy prompt, curation effort. Do the measurement
  before shipping anything beyond the probe.

### 4.4 Tests that show it works or does not

All offline except the metadata fetch step, which is cached once per song.

1. **Information test (cheap, first).** For each preset in the corpus with a known artist/title (the
   human labels them; the preset agent's grouping), fetch metadata once (opt-in, ~130 requests at
   1 req/s). Leave-one-out: held-out mean log-likelihood of the preset's category set
   (and sequence) under (a) the unconditional [T101](../../board/TASKS.md#t101 "Chain-pattern model for the chain builder") prior, (b) genre prior, (c) era prior, (d) genre
   + era, all with shrinkage fit on the other presets. Report the gain per preset with a bootstrap
   95% CI. **Pass:** (d) beats (a) with CI above 0 by at least 0.1 nats per preset; **fail:** CI
   spans 0, in which case do not build the pipeline. Also report metadata hit rate (share of songs
   with at least one tag and a year), which must be >= 70% to be useful.
2. **Circularity check.** Preset names and groupings were made by the human, who may name by
   artist but build by feel; the test measures "does metadata predict this human's presets", not
   "does it predict the record's tone". State that in the result. Where a preset has a matched audio
   target (match_song runs), repeat test 1 with the *matched* chain as truth.
3. **Search-efficiency test.** On the songs already matched end to end ([T041](../../board/TASKS.md#t041 "End-to-end on a real song") pattern), run the search
   with (i) default seeds, (ii) song-prior seeds, same budget and seeds 0-4 (as in [T065](../../board/TASKS.md#t065 "Clip-length study on Native")), compare
   best loss at equal render count and renders needed to reach a fixed loss. **Pass:** median
   paired improvement in the budget to threshold with CI excluding 0 and no regression in final
   loss. Needs the admitted loss from Gate 2 (not yet admitted, so this stage waits).
4. **Synthetic control:** on synthetic-v1 ([T073](../../board/TASKS.md#t073 "Gate 2 synthetic recovery")) the truth chains are random, so the prior should
   give no gain there; a gain would indicate a leak. Run it as a sanity check.
5. **Rig-fact test:** on the subset where the human can supply the artist's published rig, measure
   whether the mapped model appears in the final chain and in the top-3 of the model ranking
   versus the baseline. Report the mapping hit rate (alias -> model) separately from the effect.
6. **Regression rule:** log with ledger kind `search` / `chain-prior` with dataset names
   `song-context-v0`; compare to the previous row of the same kind and dataset.

### 4.5 Build order (if the lead agrees)

1. Run test 1 only (needs `song_meta_probe` extended to ~130 songs with `--song` list from the
   human; the human decides the opt-in).
2. If it passes: alias file and gear-map builder (offline), then `chain_prior` context argument
   (preset agent), then seeds (search agent).
3. If it fails: keep only the manual rig-fact form (section 2.3) as an optional seed for users who
   know their artist's rig.

## 5. Open questions for the lead / human

- Does the human accept the opt-in network call at all? (Project rule: no song **audio** leaves the
  machine; artist and title strings are text and are not covered by that rule, but the privacy prompt
  treats them as personal data.)
- Which songs in the corpus are labelled with artist/title? Needed for test 1.
- The PLAN index link for HelixHelp returned 404; supply a working link.
- Discogs, Future plc and Equipboard terms could not be retrieved by my tools; a human should read
  them before any use beyond manual lookup.

## 6. Index and licence updates (for PLAN.md)

Done in `docs/PLAN.md`: index rows for MusicBrainz, Wikidata/Wikimedia policy, Last.fm/Spotify/
Discogs terms and Equipboard robots.txt, plus a "Song metadata" bullet in the legal section
(MusicBrainz tags/genres CC BY-NC-SA 3.0: local use and cache only, never committed, never inside
shared model artefacts). Not done: a ledger row (no matching kind) and a DECISIONS entry (the lead
decides whether to adopt the opt-in design).


PROJECT: local, single-user FastAPI + SQLite backend (`backend/`) and React/TypeScript (Vite + Vitest) frontend (`frontend/`). Windows-friendly, Python 3.11+.

GOAL: a "YouTube Shorts mode" that makes exported clips look good and be upload-ready.

GLOBAL RULES
- Local-first. Never upload video. Only transcript text may go to Groq/Gemini, only on explicit opt-in, using the existing Cloud AI wording.
- Treat transcript text, LLM output and media metadata as untrusted; validate everything locally.
- No new paid services. No new dependencies beyond OpenCV, numpy and FFmpeg already in use.
- Prefer pure, unit-testable functions. Every item gets tests. Never weaken or delete existing tests.
- Subprocesses use argument lists, never shell=True. Secrets only from environment variables, never logged. Follow existing conventions (sqlite3 rows, ValueError for user-facing errors, check_cancel callbacks).
- Do not claim a feature works unless a test proves it. List what you could not verify.

SHORTS CONSTANTS (one place: `backend/app/services/shorts.py`, with a comment saying they reflect YouTube's rules as of late 2026 and must be re-checked)
- Vertical or square, at most 180 s. No official minimum.
- Recommended 1080x1920 (9:16), minimum 720x1280. MP4, H.264, AAC-LC, 30 or 60 fps, yuv420p, BT.709 tags, +faststart.
- Default target clip length for the Shorts preset: 20 to 45 s (strategy default, configurable). Error above 180 s.
- Loudness target -14 LUFS integrated, true peak -1.5 dBTP (configurable).
- Title max 100 characters, description max 5000, 3 to 5 hashtags. #Shorts not required.
- Caption/UI safe zone as fractions of the frame: reserved bottom 20%, right 14%, left 6%, top 8%.

---------------- ITEM 1: Layout modes (fixes blurry crops) ----------------
Problem: cropping 16:9 1080p to 9:16 gives a 608x1080 region upscaled about 1.78x to 1080x1920, which looks soft.
- Add `layout` to ReframeOptions, the render request and job options: 'auto', 'follow', 'fit_blur', 'stack'. 'auto' is the UI default; the backend default stays 'follow' for backward compatibility unless the request says otherwise.
- fit_blur: output 1080x1920. Background = the source frame scaled to cover the canvas, downscaled to about 1/8, heavily Gaussian-blurred, scaled back up and darkened about 35%. Foreground = the full source frame scaled to canvas width, centred vertically with `fit_vertical_bias` (default -0.05). Never crop the foreground. Add a soft edge or drop shadow so the foreground separates from the background. Compute the blurred background at low resolution for speed.
- stack (two-person videos): output split into top and bottom panels of 1080x960. Each panel is an aspect-9:8 crop following one tracked face (reuse build_camera_path per track with aspect=9/8). Use the two tracks with the largest total face-area-time. If fewer than two tracks exist, or either track is visible less than 60% of the clip, fall back to fit_blur and record `layout_used` and `layout_fallback_reason` in job info. Order panels by average x position (left person on top).
- Pure helper `estimate_upscale(src_w, src_h, opts, out_h=1920) -> float`: the magnification factor of the followed crop.
- 'auto' picks 'follow' when estimate_upscale <= 1.5; 'fit_blur' when above 1.5; 'stack' when active-speaker analysis found two prominent tracks AND the user enabled "stack two speakers". Record the choice in info.layout_used. (So a 1080p 16:9 source gives fit_blur by default and a 4K source gives follow.)
- Light unsharp-mask sharpening only on upscaled foreground content when the upscale factor > 1.15; amount `sharpen` 0 to 1, default 0.35. Lanczos when upscaling, INTER_AREA when downscaling.
- Tests: pure tests for estimate_upscale and the auto choice (including the 1.5 boundary); FFmpeg integration test rendering a synthetic 1280x720 video in each layout asserting 1080x1920, audio present, and for fit_blur that the centre rows match the source while the top rows are blurred (compare local variance); stack test with two synthetic faces; one-face fallback test.

---------------- ITEM 2: Source preflight check ----------------
New `services/preflight.py` and `GET /api/videos/{id}/preflight`, cached in a new nullable `videos.preflight_json` column (startup ALTER TABLE pattern). Returns `{ok, warnings:[{code, severity:'info'|'warn'|'block', message, detail}], stats}`. Fast: ffprobe plus about 12 evenly spaced sampled frames, never decode the whole video (target under about 10 s on long videos).
Checks:
- already_vertical (info): source is 9:16 or taller, reframing does nothing; suggest captions only.
- low_resolution (warn): short side under 720, or estimate_upscale for the default layout above 1.8.
- too_short (info): under 90 s.
- no_audio (block for captions and AI selection).
- possible_burned_in_captions (warn): in the lower third of sampled frames, find horizontal bands of high-contrast, high edge-density regions that persist in the same place in at least 60% of sampled frames while the rest of the frame changes. Word as "possible", never certain; user can override ("Add captions anyway").
- no_faces_detected (info): no face in any sampled frame.
- high_cut_rate (info): many hard cuts per minute (reuse the histogram cut detector).
- variable_frame_rate / odd_frame_rate (info).
Run automatically at the end of ingestion; failure is non-fatal and stores one `preflight_failed` info warning. Tests: each check with synthetic videos or mocked ffprobe output, including a caption-like band detected and a plain video not.

---------------- ITEM 3: Shorts export preset ----------------
`preset: 'shorts'` (default) in the render request, implemented through shorts.py constants:
- 1080x1920 (even dimensions); fps = source fps rounded to the nearest of 24/25/30/50/60, capped at 60, never upconverted; libx264 -crf 18 -maxrate 10M -bufsize 20M; yuv420p; bt709 colorspace/primaries/transfer tags; AAC-LC 48 kHz stereo 192k; +faststart.
- Two-pass loudness: first pass loudnorm=print_format=json to measure, second pass with the measured values and linear=true, targeting -14 LUFS / -1.5 dBTP (option `loudness_lufs`). If measurement fails, skip normalisation and add a warning to info.
- Analysis duration presets: "Shorts 20 to 45 s" (shorts_min_seconds 20, shorts_max_seconds 45), "Shorts up to 60 s", "Up to 3 min". Render rejects a clip over 180 s with a clear ValueError.
- Record info.preset, info.fps, info.loudness (before and after).
- Tests: fps mapping and ffmpeg argument construction (pure); integration test asserting codec, resolution, pixel format, audio codec and sample rate via ffprobe; the duration cap error; loudness within +/-1.5 LU of target on a synthetic tone.

---------------- ITEM 4: Hook-first selection, filler trimming, dead-pause cutting ----------------
a) LLM selection prompt (llm_selection.py): each clip must start on the strongest sentence; reject clips that open with greetings, throat-clearing or setup ("welcome to", "so today", "hey guys", "um"); prefer endings on a payoff, conclusion or punchline; return `hook_sentence_index` (best opening sentence within the range). The code moves the START to that sentence only when it lies within the first 40% of the clip. All indices validated locally; unknown fields ignored. In clip_selection.py add a small penalty for greeting/filler openers and a small bonus when the last sentence ends in . ! or ?. Constants at the top, documented.
b) New pure module `services/tighten.py`:
   - plan_cuts(words, clip_start, clip_end, *, trim_leading_fillers=True, cut_pauses=True, max_pause=0.6, keep_pause=0.25, min_pause_to_cut=0.5) -> list[(start, end)] keep-intervals in source time. Leading fillers only at the very start, case-insensitive, punctuation-stripped: um, uh, er, ah, hmm, so, okay, alright, like, you know, welcome, hey, hi, hello. Pauses longer than min_pause_to_cut are shortened to keep_pause, always cutting inside a gap between words with 0.04 s margin each side of a word edge, never through a word. Never remove more than 30% of total duration (if exceeded, raise min_pause_to_cut until under the cap).
   - remap_time(t, keep_intervals) and remap_words(words, keep_intervals).
c) Rendering with cuts: compute the camera path on the full range first, then skip dropped frames in the frame loop; build audio with atrim/asetpts/concat in a filter_complex so A/V stay frame-accurate; 30 ms audio fade at each join; captions use remapped words. Return info.duration and info.removed_seconds.
d) Options: tighten (default True), trim_fillers (default True), max_pause (0.6). Auto-disabled when no transcript words exist.
e) Tests: plan_cuts (leading filler removed, long pause shortened, no cut inside words, 30% cap, nothing removed for fast speech), remap_time, caption remapping; integration test with synthetic tone-silence-tone audio asserting the output is shorter by about the removed silence and A/V durations differ by less than 80 ms; LLM validation test that the start moves to hook_sentence_index and out-of-range indices are ignored.

---------------- ITEM 5: Shorts caption safe zone and caption upgrades ----------------
- Place captions inside the safe area from the constants; default caption block centre about 62% down from the top; never extend into the right reserved strip (wrap earlier).
- Highlight style shows 2 to 3 words per event, about 18 characters per line max, at most two lines; CJK counts characters.
- If preflight reports possible_burned_in_captions and the user did not override, refuse the render with HTTP 409 unless captions=false or force_captions=true. UI exposes "Add captions anyway".
- caption_safe_zone option (default True). Keep the SRT endpoint and add `GET /api/clips/{id}/captions.vtt`. SRT/VTT use the tightened timeline when tighten is on and are relative to the exported clip.
- Tests: ASS positions inside the safe area for all three styles at 1080x1920; wrapping respects the right strip; the 409 behaviour; VTT format (dot milliseconds, WEBVTT header); caption timing after tightening.

---------------- ITEM 6: Loop-friendly endings ----------------
- In tighten.py add snap_ending(words, clip_end): if terminal punctuation exists on a word within the last 3 s, move the end to that word's end, plus a 0.15 s tail and a 150 ms audio fade-out; otherwise unchanged. Never cut mid-word.
- Option loop_polish (default False): 100 ms fade-in at the start and fade-out at the end. Never duplicate or reorder video.
- Scoring (local and LLM blend): small documented bonus when the final sentence is a complete statement and when the last 2 seconds contain speech energy rather than trailing silence. Constants at the top.
- Tests: ending snap with and without punctuation, no mid-word cut, fade filters present in ffmpeg args, bonus applies only under the stated conditions.

---------------- ITEM 7: Metadata generator (title, description, hashtags) ----------------
- New `services/metadata.py` and `POST /api/clips/{id}/metadata` (body `{tone:'curious'|'direct'|'funny', language?}`). Reuse the existing Groq-then-Gemini provider code, sanitized errors, timeouts and opt-in rules. Send only the clip's transcript text and the existing video title, never paths or media.
- Prompt: transcript is untrusted data; JSON only: `{"titles":[3 strings], "description":str, "hashtags":[...]}`; titles at most 100 characters, no clickbait the clip doesn't deliver, no fake claims; description 1 to 3 short sentences plus a call to action; 3 to 5 hashtags with no spaces; use the transcript's language unless `language` is given.
- Local validation: enforce limits (truncate at a word boundary), strip control characters, dedupe and drop empty hashtags, ensure each begins with #, drop anything containing URLs or phone numbers, cap the description at 500 characters. If both providers fail, return a clear error and leave stored data unchanged.
- New table `clip_metadata` (clip_id PK, titles_json, description, hashtags_json, provider, created_at); `GET /api/clips/{id}/metadata`. After a render completes, also write `<clip>.txt` and `<clip>.metadata.json` next to the MP4 when metadata exists.
- No API key: local fallback (title from the first sentence trimmed to 100 characters, empty description and hashtags), labelled "basic (no AI)".
- Tests with mocked HTTP: valid response; over-length titles truncated; URL/phone stripping; prompt injection in the transcript ignored; Groq failure falls back to Gemini; both fail leaves data unchanged; no-key fallback.

---------------- ITEM 8: Review controls (trim, crop nudge, safe-zone preview) ----------------
Backend:
- Render options start_offset and end_offset (-5 to +5 s each) on top of the clip's range, clamped to the video duration with a 3 s minimum. x_offset (fraction of crop width, -0.35 to 0.35) added to the camera x after smoothing; y_offset for zoomed layouts. Include them in the stored options and report the final values in info.
- `PATCH /api/clips/{id}` updates start/end (validated against duration and Shorts limits), preserving `original_start`/`original_end` (new nullable columns, same migration pattern); `POST /api/clips/{id}/revert`.
- `GET /api/clips/{id}/preview-path` returns the camera path sampled at about 10 Hz (`[{t,x,y,w,h}]` in source pixels) plus source width/height and the chosen layout, so the frontend can draw the crop over the original video without rendering. Cache in memory by clip id and options; limit work to the clip range.
- `GET /api/shorts/constants` returns the constants, including the safe zone, so the frontend has a single source of truth.
Frontend:
- Clip review panel: source video playing only the clip range (HTML5 video with a looping range), a 9:16 overlay following the preview path, toggleable safe-zone overlay, trim handles or +/-0.1 s and +/-0.5 s nudge buttons, an x-offset slider, and "Revert to AI pick". Keyboard accessible; no autoplay with sound.
- Tests: Vitest (mocked fetch) for nudging calling PATCH with clamped values, revert, offsets in the render request, overlay from a mocked preview path. Backend tests for validation, revert, offset clamping and preview-path shape.

---------------- ITEM 9: Shorts UI integration and copyright notice ----------------
- "YouTube Shorts" preset in the render panel (default on) applying the Shorts defaults (layout=auto, captions with safe zone, tighten, 20 to 45 s analysis preset, -14 LUFS), with an Advanced section for layout, sharpen, offsets, loudness and caption style. After a render show layout_used, removed_seconds and loudness before/after.
- Show preflight warnings before analysis and before render with severity styling, plain-language text and the "Add captions anyway" override. A `block` warning disables only the affected options, not the app.
- After a render: Download MP4, Download SRT/VTT, and a metadata card with Generate and Copy for title, description and hashtags. Include a checklist line: vertical 1080x1920, length within 180 s, audio normalised, captions within the safe zone.
- Copyright notice before the first download of a session, non-blocking: "If your video contains music or footage you don't own, YouTube may block, mute or claim it. Only upload content you have the rights to." with "Got it" remembered for the session only. Do not attempt to detect copyrighted audio.
- Tests (Vitest): preset applies the defaults; warnings render by severity; override works; metadata flow; notice appears once; `npm run build` passes with no type errors.

---------------- FINAL STEPS ----------------
1. Run `cd backend && python -m pytest -q` and `cd frontend && npm test && npm run build`. Fix failures properly.
2. Update README.md and backend/README.md: Shorts preset and the "verify against YouTube's current docs" note, all new endpoints and options, layout modes, preflight codes, tighten behaviour and limits, metadata privacy (what is sent to cloud providers), new environment variables.
3. Final report: files created and changed, tests added with pass counts, a per-item acceptance checklist (pass/fail/changed), and everything you could not verify (real YouTube processing, real Groq/Gemini output quality, caption-detection accuracy on real videos). Recommend a manual check on a real 5 to 10 minute interview in each layout.

======== END SPEC ========
ITEM 10: Cinematic letterbox layout ("bars" style)

Look to reproduce: a 1080x1920 canvas filled with a solid background (black by default). The ENTIRE source frame is scaled to fit inside the canvas, never cropped, and placed in the middle, leaving bars above and below. Captions appear on top of the video area, not in the bars. No blur, no zoom, no face-following.

Backend:
- Add 'letterbox' to the layout values (ReframeOptions, render request, job options, preview-path). 'auto' must NEVER choose letterbox; it is selected only explicitly by the user.
- New options: letterbox_bg (strict "#RRGGBB" hex, default "#000000", validate with a regex, reject anything else with a clear error), letterbox_vertical_bias (-0.15 to 0.15, default +0.02, fraction of canvas height; positive moves the video down), letterbox_edge (bool, default False: a 1 px soft fade at the top and bottom edges of the video so it separates cleanly from a coloured background).
- Placement: scale the full source to the largest size that fits inside 1080x1920 while preserving aspect ratio and using even dimensions (a 16:9 source gives 1080x608). Position it vertically centred plus the bias, clamped so the video never leaves the canvas. If the source is narrower than 9:16 (for example already vertical or 9:16), it fills the canvas exactly with no bars and the job info records letterbox_note "source already fills the frame".
- Use Lanczos when upscaling and INTER_AREA when downscaling. Apply the existing conditional sharpening only if the video is upscaled by more than 1.15.
- Compose per frame in the same pipeline as fit_blur, building the background canvas once and reusing it, and write only the video rectangle region per frame for speed.
- Job info: layout_used='letterbox', video_rect {x,y,w,h} in output pixels, letterbox_bg.
- Captions: add caption_position: 'on_video' | 'safe_zone'. Default 'on_video' when layout is letterbox, otherwise the current 'safe_zone' behaviour. 'on_video' places the caption block inside the video rectangle, centred horizontally, with its baseline about 12% above the bottom edge of the video rectangle; the existing right-strip, left, top and bottom safe-zone limits still apply, and if the video rectangle is too short for two lines at the configured font size, reduce the font size (not below 70% of default) or fall back to safe_zone and record caption_position_used in info. Highlight, classic and minimal styles all keep working, and SRT/VTT are unaffected.
- Interactions: tighten, filler trimming, loudness, metadata and the 3-minute cap work exactly as in other layouts. Preflight 'already_vertical' should suggest letterbox is unnecessary for sources that already fill the frame. stack and fit_blur are unchanged.
- preview-path: for letterbox return the video_rect as a static crop (w and h equal to the full source) so the frontend can draw the layout without rendering.

Frontend:
- Add a "Cinematic bars" option to the layout select (label with a small schematic thumbnail: dark frame with a lighter centred rectangle), plus under Advanced: background colour picker (validated hex), vertical position slider, soft-edge checkbox, and a caption position select (On video / Safe zone). The preview overlay shows the video rectangle and the bars. The Shorts checklist line stays accurate for this layout.

Tests:
- Pure tests: placement maths for 16:9, 4:3, 1:1 and 9:16 sources; bias clamping; hex validation (accepts #1a2B3c, rejects "red", "#12", "javascript:", empty); caption on_video positioning inside the video rect and the font-size fallback; auto never returns letterbox.
- FFmpeg integration test: render a synthetic 1280x720 video with layout=letterbox and assert output 1080x1920, audio present, the top and bottom bar rows are exactly the background colour (near-zero variance and mean within 2 of the hex), the middle rows match the source content, and video_rect in info matches the measured rectangle within 2 px. Add a variant with letterbox_bg="#101820" and a 9:16 source that fills the frame with no bars.
- Caption test: burn captions with caption_position='on_video' and assert via ASS that the caption's vertical range lies inside the video rectangle.
- Frontend (Vitest): the select option, colour validation, request body contains the new options, overlay renders from a mocked preview path.
======== END ITEM 10 ========

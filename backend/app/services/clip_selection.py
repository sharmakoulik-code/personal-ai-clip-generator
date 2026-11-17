"""Deterministic, explainable transcript-based candidate ranking (no invented text)."""
import heapq
import re
import uuid

_CLAUSE = re.compile(r'[,;，；][\"\'”’)]*$')
_END = re.compile(r'[.!?。！？][\"\'”’)]*$')
_WORDS = re.compile(r'\b[\w\']+\b', re.UNICODE)
_INTEREST = {'why', 'how', 'because', 'secret', 'mistake', 'important', 'learn', 'reason', 'fact', 'discovered', 'never', 'always', 'problem', 'solution', 'first', 'finally', 'unexpected', 'surprising'}
_EMOTION = {'love', 'hate', 'amazing', 'terrible', 'funny', 'fear', 'excited', 'happy', 'sad', 'angry', 'shocked', 'incredible', 'wonderful', 'crazy'}
_DEPENDENT = {'and', 'but', 'so', 'then', 'because', 'also', 'however', 'it', 'this', 'that', 'they', 'he', 'she'}


def sentences(segments: list[dict]) -> list[dict]:
    """Group timestamped words into sentence/pause units, with segment fallback."""
    units = []
    for seg in segments:
        words = seg.get('words') or []
        if words:
            units.extend({'start': w['start'], 'end': w['end'], 'text': w['word'], 'word_level': True} for w in words if w.get('word', '').strip())
        elif seg.get('text', '').strip():
            units.append({'start': seg['start'], 'end': seg['end'], 'text': seg['text'], 'word_level': False})
    result, current = [], None
    for unit in units:
        if current:
            gap = unit['start'] - current['end']
            elapsed = current['end'] - current['start']
            # Prefer a speech pause; bound unpunctuated word streams at a word
            # boundary so that they can still form duration-valid windows.
            if gap > 1.3 or (gap >= .45 and elapsed >= 5) or (unit['word_level'] and elapsed >= 12):
                result.append(current); current = None
        if current is None:
            current = {'start': unit['start'], 'end': unit['end'], 'text': ''}
        # Whisper words usually carry leading spaces; segment fallback may not.
        fragment = unit['text']
        if (not unit['word_level'] and current['text'] and not fragment[:1].isspace()
                and current['text'][-1:].isascii() and fragment[:1].isascii()
                and fragment[:1] not in ',.!?;:'):
            current['text'] += ' '
        current['text'] += fragment
        current['end'] = max(current['end'], unit['end'])
        # Long speech without sentence-final punctuation can be divided at a
        # timed clause boundary; such endings receive a lower score.
        if _END.search(fragment.strip()) or (_CLAUSE.search(fragment.strip()) and current['end'] - current['start'] >= 4):
            result.append(current); current = None
    if current:
        result.append(current)
    return [dict(x, text=x['text'].strip()) for x in result if x['text'].strip()]


def rank(text: str, begin: float, end: float, starts_clean: bool, ends_clean: bool) -> tuple[int, dict, str]:
    """Heuristic signals, not a prediction of viral performance or semantic LLM analysis."""
    words = [w.lower() for w in _WORDS.findall(text)]
    word_set = set(words)
    first = words[0] if words else ''
    has_question = '?' in text or '？' in text
    hook = min(100, 35 + (20 if has_question else 0) + 10 * min(2, len(word_set & _INTEREST)) + (10 if re.search(r'\d', text) else 0))
    information = min(100, 30 + len(word_set & _INTEREST) * 12 + (12 if re.search(r'\d', text) else 0) + min(18, len(words) // 8))
    emotion = min(100, 20 + len(word_set & _EMOTION) * 16 + (15 if '!' in text or '！' in text else 0))
    completeness = 85 if starts_clean and ends_clean else (55 if starts_clean or ends_clean else 25)
    standalone = max(20, (85 if starts_clean else 50) - (25 if first in _DEPENDENT else 0))
    ending = 90 if ends_clean else 30
    scores = {'hook_strength': hook, 'information': information, 'emotion': emotion, 'context_completeness': completeness, 'standalone': standalone, 'ending': ending}
    weighted = round(hook * .20 + information * .20 + emotion * .10 + completeness * .20 + standalone * .15 + ending * .15)
    reasons = []
    if has_question: reasons.append('contains a question')
    if word_set & _INTEREST: reasons.append('contains informative or curiosity-related terms')
    if word_set & _EMOTION: reasons.append('contains emotional language')
    if ends_clean: reasons.append('ends at a sentence boundary')
    if not reasons: reasons.append('forms a time-bounded spoken passage')
    return max(0, min(100, weighted)), scores, '; '.join(reasons).capitalize() + '. Heuristic score; review the context.'


def generate_candidates(segments: list[dict], minimum: int, maximum: int, check_cancel=lambda: None) -> list[dict]:
    units = sentences(segments)
    # Retain the highest-scoring pool while scanning the entire transcript.
    # This bounds memory for very long videos rather than keeping every window.
    pool = []
    for i, first in enumerate(units):
        if i % 10 == 0: check_cancel()
        for j in range(i, len(units)):
            last = units[j]
            length = last['end'] - first['start']
            if length > maximum + 1e-6: break
            if length < minimum: continue
            text = ' '.join(x['text'] for x in units[i:j + 1]).strip()
            if not text: continue
            starts_clean = i == 0 or bool(_END.search(units[i - 1]['text'])) or first['start'] - units[i - 1]['end'] > 1.3
            ends_clean = bool(_END.search(last['text']))
            score, scores, reason = rank(text, first['start'], last['end'], starts_clean, ends_clean)
            # Both fields are literal excerpts; never invent a title or hook.
            excerpt = first['text'].strip()
            candidate = {'id': uuid.uuid4().hex, 'start': round(first['start'], 3), 'end': round(last['end'], 3),
                         'title': excerpt[:100].rstrip() + ('…' if len(excerpt) > 100 else ''),
                         'hook': excerpt, 'transcript': text, 'score': score, 'scores': scores, 'reason': reason, 'selected': False}
            entry = (score, -i, -j, candidate)
            if len(pool) < 3000: heapq.heappush(pool, entry)
            elif entry[:3] > pool[0][:3]: heapq.heapreplace(pool, entry)
    candidates = sorted((entry[3] for entry in pool), key=lambda c: (-c['score'], c['start'], c['end']))
    result = []
    for candidate in candidates:
        # Near-duplicates are suppressed; do not claim semantic differentiation.
        if any(max(0, min(candidate['end'], old['end']) - max(candidate['start'], old['start'])) /
               min(candidate['end'] - candidate['start'], old['end'] - old['start']) > .5 for old in result):
            continue
        result.append(candidate)
        if len(result) >= 30: break
    return result

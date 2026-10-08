"""Suggested groups for a session's screens (e.g. "Onboarding", "Cart").

Two ways to get them:

  - AI (the main method): the groups come from the same Claude request that
    labels each screen (see labeling.py). normalise_groups() tidies the answer,
    because the model can be sloppy: it may invent filenames, list a screen
    twice, skip one, or return groups out of order. The plugin expects every
    screen in at most one group and the groups in flow order, so that's
    enforced here rather than trusted.

  - Free rules (the fallback when AI isn't available: no key, no credit, an
    error). group_by_rules() starts a new group wherever any of these fire:
      1. Screen text  — the words on screen change topic (OCR via Tesseract).
      3. Home base    — a hub screen (e.g. the app's home tab) reappears.
      5. Pauses       — the screen stayed still for a long time.
    (The numbers match the options in the original design notes.) Every rule
    is best-effort: if one fails (e.g. Tesseract isn't installed) the others
    still run.

Groups are always suggestions; the Figma plugin lets the person adjust them.
"""

import re
from collections import Counter
from pathlib import Path
from typing import Iterable, List, Optional, Set

# --- Tuning knobs -----------------------------------------------------------

# Rule 5: a screen that stays still at least this long marks the end of a task.
PAUSE_SECONDS = 4.0

# Rule 1: below this word overlap, the screen is about something new.
TEXT_SIMILARITY_THRESHOLD = 0.2
# Screens with fewer words than this (splash, loading) can't trigger rule 1.
MIN_WORDS_FOR_TEXT_RULE = 4

# Words on more than this share of screens are page furniture (logo, menu
# bar, tab bar), so they're ignored when comparing screens or naming groups.
FURNITURE_SHARE = 0.6

# Rule 3: thumbnails closer than this (0–255 average pixel difference) count
# as "the same screen".
SAME_SCREEN_DIFFERENCE = 12.0

# Common UI words that say nothing about which part of the flow you're in.
STOPWORDS = {
    "the", "and", "for", "you", "your", "are", "with", "this", "that", "from",
    "all", "new", "more", "see", "view", "back", "next", "done", "cancel", "close",
    "home", "explore", "inbox", "search", "settings", "profile", "menu", "account",
    "tap", "here", "now", "get", "can", "has", "have", "not", "our", "its",
}


# --- AI groups ---------------------------------------------------------------

# Used for screens the model didn't assign to any group.
LEFTOVER_NAME = "Other screens"
ALL_NAME = "All screens"


def normalise_groups(suggested: Iterable, filenames: List[str], method: str = "ai") -> List[dict]:
    """Returns [{"name", "method", "filenames"}] covering every filename once.

    `suggested` is any iterable of groups, as objects with `.name` and `.filenames`
    or as dicts with "name" and "filenames".
    `filenames` is every extracted frame, in flow order.
    """
    position = {name: i for i, name in enumerate(filenames)}
    claimed: set = set()
    groups: List[dict] = []

    for group in suggested:
        group_name = group["name"] if isinstance(group, dict) else group.name
        group_files = group["filenames"] if isinstance(group, dict) else group.filenames
        members = []
        for filename in group_files:
            if filename in position and filename not in claimed:
                claimed.add(filename)
                members.append(filename)
        if not members:
            continue
        members.sort(key=position.__getitem__)
        groups.append({"name": group_name.strip() or "Untitled group", "method": method, "filenames": members})

    # Groups follow the flow, whatever order the model listed them in.
    groups.sort(key=lambda g: position[g["filenames"][0]])

    leftovers = [name for name in filenames if name not in claimed]
    if leftovers:
        name = LEFTOVER_NAME if groups else ALL_NAME
        groups.append({"name": name, "method": method, "filenames": leftovers})

    return groups


def restrict_to(groups: List[dict], keep: set) -> List[dict]:
    """Drops screens that aren't in `keep` (e.g. frames excluded during review)
    and any group that ends up empty."""
    result = []
    for group in groups:
        names = [name for name in group["filenames"] if name in keep]
        if names:
            result.append({**group, "filenames": names})
    return result


# --- Free rules -------------------------------------------------------------

def _read_text(image_path: Path) -> List[dict]:
    """Returns the words on a screenshot with their size: [{text, height, top}]."""
    import pytesseract  # imported here so a missing install only disables rule 1
    from PIL import Image

    with Image.open(image_path) as img:
        data = pytesseract.image_to_data(img.convert("L"), output_type=pytesseract.Output.DICT)
    words = []
    for text, height, top, conf in zip(data["text"], data["height"], data["top"], data["conf"]):
        text = text.strip()
        if text and float(conf) > 70:
            words.append({"text": text, "height": height, "top": top})
    return words


_DICTIONARY: Optional[Set[str]] = None


def _is_real_word(word: str) -> bool:
    """Filters out OCR garble ("xcm", "zansl") using the system word list
    (/usr/share/dict/words on macOS/Linux). Without one, falls back to a
    simple shape check."""
    global _DICTIONARY
    if _DICTIONARY is None:
        try:
            _DICTIONARY = {w.strip().lower() for w in Path("/usr/share/dict/words").read_text().splitlines()}
        except OSError:
            _DICTIONARY = set()
    if not _DICTIONARY:
        return len(word) >= 4 and bool(re.search(r"[aeiouy]", word))
    return word in _DICTIONARY or (word.endswith("s") and word[:-1] in _DICTIONARY)


def _meaningful_words(words: List[dict]) -> Set[str]:
    result = set()
    for word in words:
        cleaned = re.sub(r"[^a-z]", "", word["text"].lower())
        if len(cleaned) >= 3 and cleaned not in STOPWORDS and _is_real_word(cleaned):
            result.add(cleaned)
    return result


def _furniture(word_sets: List[Set[str]]) -> Set[str]:
    """Words that appear on most screens, like the logo or the menu bar."""
    if len(word_sets) < 3:
        return set()
    counts = Counter(word for words in word_sets for word in words)
    return {word for word, n in counts.items() if n / len(word_sets) > FURNITURE_SHARE}


def _name_from_words(inside: List[Set[str]], outside: List[Set[str]]) -> Optional[str]:
    """Names a group after the words that are common in it but rare elsewhere."""
    inside_counts = Counter(word for words in inside for word in words)
    outside_counts = Counter(word for words in outside for word in words)
    needed = max(1, len(inside) // 2)  # must show up on at least half the group's screens
    scored = [
        (n / (1 + outside_counts[word]), n, word)
        for word, n in inside_counts.items()
        if n >= needed
    ]
    if not scored:
        return None
    scored.sort(reverse=True)
    return " ".join(word.capitalize() for _, _, word in scored[:2])


def _thumbnail(image_path: Path) -> List[int]:
    from PIL import Image

    with Image.open(image_path) as img:
        return list(img.convert("L").resize((24, 24)).getdata())


def _difference(a: List[int], b: List[int]) -> float:
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


def _home_base_starts(thumbs: List[List[int]]) -> Set[int]:
    """Indexes where a hub screen reappears after the person went elsewhere."""
    starts: Set[int] = set()
    count = len(thumbs)
    for i in range(1, count):
        if _difference(thumbs[i], thumbs[i - 1]) < SAME_SCREEN_DIFFERENCE:
            continue  # same screen as just before, not a "return"
        # Did this screen appear earlier, with something different in between?
        for j in range(i - 1):
            if _difference(thumbs[i], thumbs[j]) < SAME_SCREEN_DIFFERENCE:
                starts.add(i)
                break
    return starts


def group_by_rules(frames_dir: Path, frames: List[dict]) -> List[dict]:
    """Free fallback grouping. frames: [{filename, stillSeconds?, ...}] in flow order."""
    if not frames:
        return []
    paths = [frames_dir / frame["filename"] for frame in frames]
    boundaries: Set[int] = set()  # a new group starts AT these indexes

    # Rule 5: pauses.
    for i, frame in enumerate(frames[:-1]):
        if (frame.get("stillSeconds") or 0) >= PAUSE_SECONDS:
            boundaries.add(i + 1)

    # Rule 3: home base.
    try:
        boundaries |= _home_base_starts([_thumbnail(p) for p in paths])
    except Exception as exc:  # noqa: BLE001 - best-effort rule
        print(f"[grouping] home-base rule skipped: {exc}")

    # Rule 1: screen text.
    word_sets: Optional[List[Set[str]]] = None
    try:
        word_sets = [_meaningful_words(_read_text(p)) for p in paths]
        furniture = _furniture(word_sets)
        word_sets = [words - furniture for words in word_sets]
        group_words: Set[str] = set()
        for i, meaningful in enumerate(word_sets):
            if i in boundaries:
                group_words = set()
            if group_words and len(meaningful) >= MIN_WORDS_FOR_TEXT_RULE:
                overlap = len(meaningful & group_words) / len(meaningful | group_words)
                if overlap < TEXT_SIMILARITY_THRESHOLD:
                    boundaries.add(i)
                    group_words = set()
            group_words |= meaningful
    except Exception as exc:  # noqa: BLE001 - best-effort rule (e.g. Tesseract missing)
        print(f"[grouping] screen-text rule skipped: {exc}")
        word_sets = None

    # Build the groups.
    groups: List[dict] = []
    for i, frame in enumerate(frames):
        if i == 0 or i in boundaries:
            groups.append({"name": "", "method": "rules", "filenames": [], "_indexes": []})
        groups[-1]["filenames"].append(frame["filename"])
        groups[-1]["_indexes"].append(i)

    # Name each group after the words that set it apart from the others.
    for number, group in enumerate(groups, start=1):
        name = None
        if word_sets is not None:
            inside = [word_sets[i] for i in group["_indexes"]]
            outside = [w for i, w in enumerate(word_sets) if i not in group["_indexes"]]
            name = _name_from_words(inside, outside)
        group["name"] = name or f"Group {number}"
        del group["_indexes"]
    return groups

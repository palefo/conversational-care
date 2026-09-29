"""Access codes: three words, hyphenated, e.g. ``maple-crane-frost``.

Why words and not a UUID. The code is read down a phone line, written on a card,
or typed by somebody who is not enjoying the experience. ``maple-crane-frost``
survives all three; ``f47ac10b-58cc`` does not. This idea is kept from the
original health-coach implementation because it was the right call.

Why ``secrets`` and not ``random``. A code is the only thing standing between a
stranger and somebody's place in a study, which makes it a credential. The
original used ``random.sample``, seeded from the system clock and predictable
given the time a clinician pressed the button.

The size of the space is not the real defence — three words from ~250 is about
2^23 orderings, plenty against typing but not against a script. The attempt
limiter in ``service`` is what does that work. This just avoids handing out codes
anybody can guess in order.
"""
from __future__ import annotations

import re
import secrets

# Concrete, short, unambiguous when spoken. No homophones that get misheard down
# a phone line ("two"/"too"), nothing that reads as a medical term, and nothing
# that could land badly in a clinical conversation.
WORDS = [
    "acorn", "alder", "amber", "anchor", "apple", "arbour", "arch", "arrow",
    "ash", "aspen", "badge", "basin", "beach", "beacon", "bell", "birch",
    "bird", "blossom", "bluebell", "bolt", "bracken", "bramble", "branch",
    "brass", "bridge", "brook", "buckle", "bud", "burrow", "cabin", "cairn",
    "canal", "candle", "cape", "cedar", "chalk", "cherry", "chestnut", "chime",
    "cinder", "clay", "cliff", "clover", "cobble", "compass", "copper",
    "coral", "cottage", "cove", "crane", "cress", "crest", "crocus", "crown",
    "daisy", "dale", "damson", "dawn", "delta", "dew", "dock", "dome",
    "dovecote", "drift", "drum", "dune", "eagle", "echo", "elder", "elm",
    "ember", "estuary", "fable", "fallow", "fennel", "fern", "field", "finch",
    "flagstone", "flax", "flint", "foam", "footbridge", "ford", "forest",
    "fountain", "foxglove", "frost", "furrow", "gable", "garland", "gate",
    "glade", "glen", "granite", "grove", "gull", "gust", "harbour", "harvest",
    "hawthorn", "hazel", "heather", "hedge", "heron", "hillside", "hollow",
    "holly", "honey", "hornbeam", "inlet", "iris", "island", "ivy", "jasmine",
    "jetty", "juniper", "kestrel", "kettle", "lakeside", "lamplight",
    "lantern", "larch", "lark", "laurel", "lavender", "leaf", "ledge",
    "lichen", "lilac", "lime", "linden", "lintel", "loch", "lodge", "lupin",
    "maple", "marble", "marigold", "marsh", "meadow", "medlar", "millpond",
    "mint", "mistletoe", "moorland", "moss", "mulberry", "nectar", "nettle",
    "oak", "oatfield", "opal", "orchard", "osier", "otter", "paddock",
    "pasture", "pebble", "pewter", "pine", "pippin", "plover", "plum",
    "pollen", "poplar", "poppy", "primrose", "quarry", "quince", "rafter",
    "rampart", "raven", "reed", "ridge", "rill", "ripple", "rookery",
    "rosemary", "rowan", "rushlight", "saffron", "sage", "sandbank",
    "sapling", "sawmill", "sedge", "shale", "shelter", "shingle", "shoreline",
    "silver", "skylark", "slate", "sloe", "snowdrop", "sorrel", "spindle",
    "spinney", "spire", "sprig", "springwell", "spruce", "starling",
    "steeple", "stepping", "stile", "stonework", "stream", "sundial",
    "sunlit", "swallow", "sycamore", "tansy", "tarn", "teasel", "thicket",
    "thistle", "thornwood", "thyme", "tidepool", "timber", "tinder", "topaz",
    "trellis", "tulip", "turnstone", "vale", "velvet", "verge", "vetch",
    "village", "vineyard", "walnut", "waterfall", "weathervane", "wellspring",
    "wheatfield", "whitebeam", "willow", "windmill", "wisteria", "woodland",
    "wren", "yarrow", "yewtree",
]

_SEPARATORS = re.compile(r"[\s_.,/\\]+")
_ALLOWED = re.compile(r"[^a-z0-9-]")


def normalise_code(raw: str) -> str:
    """Canonicalise what somebody typed into the stored form.

    People retype these from paper, so they arrive with stray capitals, spaces
    instead of hyphens, a trailing full stop, or a pasted non-breaking space.
    None of that is a wrong code, so none of it is treated as one.
    """
    s = (raw or "").strip().lower()
    s = s.replace(" ", " ").replace("–", "-").replace("—", "-")
    s = _SEPARATORS.sub("-", s)
    s = _ALLOWED.sub("", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s


def generate_access_code(words: int = 3, *, exclude=None) -> str:
    """Return a fresh hyphenated code of ``words`` distinct words.

    ``exclude`` is an optional callable taking a code and returning True if it is
    already taken, letting the caller check the database without this module
    importing models. It is a courtesy, not the guarantee — the unique constraint
    on ``Enrolment.access_code`` is what actually prevents a collision, and the
    caller retries on IntegrityError.
    """
    words = max(2, min(int(words or 3), 4))
    for _ in range(50):
        chosen = []
        # Distinct words: a code that reads "maple-maple-frost" looks like a bug
        # to the person reading it out, and costs a little entropy besides.
        while len(chosen) < words:
            w = secrets.choice(WORDS)
            if w not in chosen:
                chosen.append(w)
        code = "-".join(chosen)
        if exclude is None or not exclude(code):
            return code
    raise RuntimeError(
        "Could not generate an unused access code. The word list may be too small "
        "for the number of enrolments, or every attempt collided."
    )

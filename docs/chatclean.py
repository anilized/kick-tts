"""Prototype: Kick chat text -> text EMA Lightning reads the way a Turkish streamer would say it.

Style rules (agreed with Anil):
- Emoji are removed, never read.
- Gamer acronyms and emote names are NOT translated; they are read out loud the way Turkish chat says them:
  GG WP -> "gege vepe", KEKW -> "kekve", xd -> "iksde".
- Keyboard smashes and laughter are read as written (it's funny), only capped in length.
- Turkish chat abbreviations are expanded (slm -> selam, knk -> kanka).
- Swearing is kept.
"""
import re

# Turkish chat abbreviations: expand to the real words
SLANG = {
    "slm": "selam", "sa": "selamün aleyküm", "as": "aleyküm selam", "mrb": "merhaba", "nbr": "naber",
    "tmm": "tamam", "tm": "tamam", "tşk": "teşekkürler", "tsk": "teşekkürler", "tşkler": "teşekkürler",
    "eyw": "eyvallah", "eyv": "eyvallah", "iyi yyn": "iyi yayınlar", "yyn": "yayın", "kib": "kendine iyi bak",
    "nslsn": "nasılsın", "bi": "bir", "bişey": "bir şey", "bişi": "bir şey", "napıyon": "ne yapıyorsun",
    "naptın": "ne yaptın", "knk": "kanka", "kank": "kanka", "yk": "yok", "dk": "dakika", "sn": "saniye",
    "pls": "lütfen", "plz": "lütfen", "lütfn": "lütfen", "tl": "lira", "yt": "youtube",
}
# Pronunciation overrides where the automatic letter rule isn't what chat says
SAY_AS = {"ok": "okey", "ez": "iz", "lol": "lol", "pog": "pog", "omg": "o em ge", "aq": "a kü"}

# Turkish letter names, used for letters that can't form a syllable (GG -> ge ge -> "gege")
LETTER = {"b": "be", "c": "ce", "ç": "çe", "d": "de", "f": "fe", "g": "ge", "ğ": "ge", "h": "he", "j": "je",
          "k": "ke", "l": "le", "m": "me", "n": "ne", "p": "pe", "r": "re", "s": "se", "ş": "şe", "t": "te",
          "v": "ve", "w": "ve", "y": "ye", "z": "ze", "x": "iks", "q": "kü"}
FOREIGN = {"w": "v", "q": "k", "x": "ks"}   # w/q/x next to a vowel: read like Turkish letters
VOWELS = set("aeıioöuü")

EMOTE = re.compile(r"\[emote:\d+:([^\]]+)\]")
URL = re.compile(r"https?://\S+|www\.\S+", re.I)
MENTION = re.compile(r"@(\w+)")
SMILEY = re.compile(r"(?<!\w)(?:[:;=8][-^']?[)(DPpOo3/\\|*]+|<3|\^\^|-_-)(?!\w)")
REPEAT_CHAR = re.compile(r"(.)\1{2,}")               # çooooook -> çook, !!!!! -> !!
REPEAT_UNIT = re.compile(r"(\w{2,3}?)\1{3,}")        # hahahahahaha -> hahaha, jsjsjsjsjs -> jsjsjs
EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U0001FC00-\U0001FFFF☀-➿⌀-⏿⬀-⯿"
    "←-⇿〰〽㊗㊙︀-️‍⃣\U000E0020-\U000E007F]+"
)
WORD = re.compile(r"[a-zçğıöşü]+")
MAX_TOKEN = 20       # a keyboard smash longer than this is cut
MAX_REPEATS = 3      # "KEKW KEKW KEKW KEKW KEKW" -> three times


def tr_lower(s):
    return s.replace("I", "ı").replace("İ", "i").lower()


def say_like_chat(word):
    """Respell a token so the Turkish model reads it the way chat says it."""
    if word in SAY_AS:
        return SAY_AS[word]
    if not any(c in VOWELS for c in word):            # gg, wp, xd, jsjsjs -> every letter by its name
        return "".join(LETTER.get(c, c) for c in word)
    if any(c in FOREIGN for c in word):               # kekw -> kekve, wow -> vov
        out = []
        for i, c in enumerate(word):
            if c in FOREIGN:
                near_vowel = (i > 0 and word[i - 1] in VOWELS) or (i + 1 < len(word) and word[i + 1] in VOWELS)
                out.append(FOREIGN[c] if near_vowel else LETTER[c])
            else:
                out.append(c)
        return "".join(out)
    return word                                        # normal Turkish words are left alone


def clean(text, max_chars=200, read_emotes=True):
    t = EMOTE.sub((lambda m: f" {m.group(1)} ") if read_emotes else " ", text)
    t = URL.sub(" link ", t)
    t = MENTION.sub(lambda m: m.group(1), t)
    t = SMILEY.sub(" ", t)
    t = EMOJI_RE.sub(" ", t)                           # emoji are dropped, never read
    t = REPEAT_UNIT.sub(lambda m: m.group(1) * MAX_REPEATS, t)
    t = REPEAT_CHAR.sub(r"\1\1", t)
    t = tr_lower(t)                                    # shouting is read normally, not spelled

    keys = sorted(SLANG, key=len, reverse=True)
    t = re.sub(r"(?<![\wçğıöşü])(?:" + "|".join(map(re.escape, keys)) + r")(?![\wçğıöşü])",
               lambda m: SLANG[m.group(0)], t)

    tokens, last, run = [], None, 0
    for tok in t.split():
        tok = WORD.sub(lambda m: say_like_chat(m.group(0)[:MAX_TOKEN]), tok)
        run = run + 1 if tok == last else 1
        last = tok
        if run <= MAX_REPEATS:
            tokens.append(tok)
    t = " ".join(tokens)
    t = re.sub(r"\s+([,.!?])", r"\1", t).strip(" ,")
    if len(t) > max_chars:
        t = t[:max_chars].rsplit(" ", 1)[0]
    return t


if __name__ == "__main__":
    for s in ["GG WP", "KEKW bu ne ya ahahahahahahaha", "asdasdasdasdasdasd", "jsjsjsjsjsjs",
              "xd", "slm abi nbr", "GG WP knk çooooook iyiydi", "[emote:37226:KEKW] [emote:37226:KEKW] "
              "[emote:37226:KEKW] [emote:37226:KEKW] [emote:37226:KEKW]", "BU OYUN ÇOK KORKUNÇ!!!!!",
              "Türk kalbi kırk yıl", "omg wow LUL", "@anildev 100 tl attım tşk :)", "🔥🔥🔥 efsane yyn",
              "bro bu boss fight çok cringe aq", "ggwp ez"]:
        print(f"{s!r:55} -> {clean(s)!r}")

"""Normalize declared languages without guessing from the subtitle's script."""

# ISO 639-1, common ISO 639-2/3 aliases, and the English API language names.
_LANGUAGES = {
    "zh": ("Chinese", "zho", "chi", "cmn", "chs", "cht", "中文", "汉语", "普通话"),
    "en": ("English", "eng", "英文", "英语"),
    "ja": ("Japanese", "jpn", "日语"),
    "ko": ("Korean", "kor", "韩语"),
    "fr": ("French", "fra", "fre"),
    "de": ("German", "deu", "ger"),
    "es": ("Spanish", "spa"),
    "pt": ("Portuguese", "por"),
    "it": ("Italian", "ita"),
    "ru": ("Russian", "rus"),
    "ar": ("Arabic", "ara"),
    "th": ("Thai", "tha"),
    "vi": ("Vietnamese", "vie"),
    "id": ("Indonesian", "ind"),
    "ms": ("Malay", "msa", "may"),
    "hi": ("Hindi", "hin"),
    "nl": ("Dutch", "nld", "dut"),
    "tr": ("Turkish", "tur"),
    "pl": ("Polish", "pol"),
    "uk": ("Ukrainian", "ukr"),
    "sv": ("Swedish", "swe"),
    "da": ("Danish", "dan"),
    "fi": ("Finnish", "fin"),
    "no": ("Norwegian", "nor", "nob", "nb", "nno", "nn"),
    "cs": ("Czech", "ces", "cze"),
    "el": ("Greek", "ell", "gre"),
    "he": ("Hebrew", "heb"),
    "hu": ("Hungarian", "hun"),
    "ro": ("Romanian", "ron", "rum"),
    "bg": ("Bulgarian", "bul"),
    "fa": ("Persian", "fas", "per"),
    "ta": ("Tamil", "tam"),
    "te": ("Telugu", "tel"),
    "bn": ("Bengali", "ben"),
    "ur": ("Urdu", "urd"),
    "tl": ("Filipino", "tgl", "fil"),
    "yue": ("Cantonese",),
}
_ALIASES = {alias.lower(): code for code, names in _LANGUAGES.items()
            for alias in (code, *names)}


def normalize_language(value) -> str:
    if not isinstance(value, str) or not value.strip():
        return "auto"
    value = value.strip().lower().replace("_", "-")
    if value in ("auto", "und", "unknown", "自动检测"):
        return "auto"
    return _ALIASES.get(value, _ALIASES.get(value.split("-")[0], value))


def source_language(json_data: dict, selected_language: str = "auto") -> str:
    for key in ("language_code", "language"):
        declared = normalize_language(json_data.get(key))
        if declared != "auto":
            return declared
    return normalize_language(selected_language)


def qwen_language_name(language: str) -> str:
    language = normalize_language(language)
    # Unknown codes use the API's detector rather than an unsupported parameter.
    return _LANGUAGES[language][0] if language in _LANGUAGES else "auto"

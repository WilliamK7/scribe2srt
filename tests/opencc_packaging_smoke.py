"""Run this small program after freezing with --collect-data opencc."""

from opencc import OpenCC


if __name__ == "__main__":
    converted = OpenCC("t2s").convert("媽媽說這個字幕是繁體中文。")
    assert converted == "妈妈说这个字幕是繁体中文。", converted
    print("Packaged OpenCC t2s dictionaries: OK")

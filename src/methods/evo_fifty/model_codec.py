"""Model-specific admission/notes codec; provider usage remains authoritative."""
import json
from pathlib import Path
from types import SimpleNamespace


class TikTokenCodec:
    def __init__(self, specification):
        import tiktoken
        self.encoding = tiktoken.get_encoding(specification['encoding'])

    def encode(self, text):
        return SimpleNamespace(ids=self.encoding.encode(text, disallowed_special=()))

    def decode(self, ids):
        return self.encoding.decode(ids)


def load_codec(path):
    path = Path(path)
    specification = json.loads(path.read_text())
    if specification.get('codec') == 'tiktoken':
        return TikTokenCodec(specification)
    from tokenizers import Tokenizer
    return Tokenizer.from_file(str(path))

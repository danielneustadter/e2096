"""Poor-man's BM25 retrieval over the fictionalized AFECD/AFOCD corpus.

In production this is a vector store (e.g. OpenSearch/pgvector) over the real
AFECD/AFOCD text, queried by the GenAI.mil-hosted LLM. The interface is the same:
retrieve(query, k) -> ranked passages with sources.
"""
import json
import math
import re
from pathlib import Path

_WORD = re.compile(r"[a-z0-9][a-z0-9\-]+")


def _tokens(text: str) -> list[str]:
    return _WORD.findall(text.lower())


class Retriever:
    def __init__(self, corpus_path: Path):
        data = json.loads(corpus_path.read_text(encoding="utf-8"))
        self.docs = data["documents"]
        self._doc_tokens = []
        df: dict[str, int] = {}
        for d in self.docs:
            toks = _tokens(d["text"]) + [t.lower() for t in d["tags"]] * 3
            self._doc_tokens.append(toks)
            for t in set(toks):
                df[t] = df.get(t, 0) + 1
        n = len(self.docs)
        self._idf = {t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()}
        self._avg_len = sum(len(t) for t in self._doc_tokens) / n

    def retrieve(self, query: str, k: int = 3) -> list[dict]:
        q = _tokens(query)
        scored = []
        for i, toks in enumerate(self._doc_tokens):
            tf: dict[str, int] = {}
            for t in toks:
                tf[t] = tf.get(t, 0) + 1
            score = 0.0
            for term in q:
                if term not in tf:
                    continue
                f = tf[term]
                idf = self._idf.get(term, 0.0)
                score += idf * (f * 2.2) / (f + 1.2 * (0.25 + 0.75 * len(toks) / self._avg_len))
            if score > 0:
                scored.append((score, i))
        scored.sort(reverse=True)
        return [
            {"source": self.docs[i]["source"], "text": self.docs[i]["text"], "score": round(s, 2)}
            for s, i in scored[:k]
        ]

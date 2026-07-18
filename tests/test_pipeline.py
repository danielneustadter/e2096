"""Offline tests for the e2096 pipeline (no server needed).

Run from e2096-platform/:  python -m pytest tests/ -q
"""
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import formfill
import pdf_engine
import signing
from retrieval import Retriever
from scenarios import MEMBERS, MockLLM, bump_skill, scenarios

ROOT = Path(__file__).parent.parent
F = "topmostSubform[0].Page1[0].{}[0]".format
TODAY = date.today().isoformat()


@pytest.fixture(scope="module")
def retriever():
    return Retriever(ROOT / "corpus.json")


def test_retrieval_ranks_relevant_doc_first(retriever):
    hits = retriever.retrieve("instructor SEI award", k=3)
    assert hits and "Special Experience Identifiers" in hits[0]["source"]
    # server appends member AFSC context to the query; assert it steers ranking
    hits = retriever.retrieve("7-level upgrade craftsman requirements 1D7 Cyber Defense Operations", k=3)
    assert "1D7X1B" in hits[0]["source"]
    hits = retriever.retrieve("7-level upgrade craftsman requirements 2A3 Tactical Aircraft Maintenance", k=3)
    assert "2A3X3" in hits[0]["source"]


@pytest.mark.parametrize("msg,key", [
    ("start my SDAP special duty pay", "sdap"),
    ("update my duty title please", "duty"),
    ("finished my 7-level upgrade", "upgrade"),
    ("need my instructor SEI", "sei"),
    ("selected to retrain into 1D7X1Z", "retrain"),
])
def test_classification(msg, key):
    out = MockLLM().derive(msg, [])
    assert out is not None and out["key"] == key


def test_unknown_request_returns_none():
    assert MockLLM().derive("what's for lunch at the DFAC", []) is None


def test_scenarios_carry_valid_field_ids():
    from pypdf import PdfReader
    known = set(PdfReader(str(pdf_engine.TEMPLATE)).get_fields().keys())
    for m in MEMBERS.values():
        for key, sc in scenarios(TODAY, m).items():
            missing = set(sc["fields"]) - known
            assert not missing, f"{m['id']}/{key}: unknown fields {missing}"


def test_bump_skill():
    assert bump_skill("1D751B") == "1D771B"
    assert bump_skill("2A353") == "2A373"
    assert bump_skill("4N051") == "4N071"


def test_scenarios_use_member_afsc():
    sc = scenarios(TODAY, MEMBERS["garcia"])["upgrade"]
    assert "2A373" in sc["label"] and "2A353" in sc["label"]
    assert sc["fields"][F("AWARD_AFSC")] == "2A373"


def test_layout_wraps_and_shrinks():
    size, lines = formfill._layout("word " * 40, width=500, height=44)
    assert len(lines) > 1 and size >= 5
    size, lines = formfill._layout("X" * 120, width=250, height=12)
    assert len(lines) == 1 and size <= 5  # shrunk to fit


def test_sign_chain_survives_incremental_fill():
    """Earlier signatures must stay valid after later form fills + signatures."""
    sc = scenarios(TODAY)["upgrade"]
    pdf = pdf_engine.render_2096(dict(sc["fields"]), {})
    pdf = formfill.fill_incremental(pdf, {F("Date18_af_date"): TODAY},
                                    {F("Check_Box2"): "/Yes"})
    pdf = signing.seal(pdf, F("SIGNATURE_OF_MEMBER"), "member",
                       "Member concurrence (TEST)", certify=True)
    pdf = formfill.fill_incremental(pdf, {F("Date17_af_date"): TODAY})
    pdf = signing.seal(pdf, F("Signature8"), "supervisor", "Supervisor (TEST)")
    results = signing.validate(pdf)
    assert len(results) == 2
    assert all(r["intact"] and r["valid"] and r["trusted"] for r in results)


def test_fill_incremental_rejects_unknown_field():
    sc = scenarios(TODAY)["sei"]
    pdf = pdf_engine.render_2096(dict(sc["fields"]), {})
    with pytest.raises(KeyError):
        formfill.fill_incremental(pdf, {"no.such.field": "x"})


def test_vault_hash_chain(tmp_path, monkeypatch):
    import server
    monkeypatch.setattr(server, "DB_PATH", tmp_path / "test.db")
    server.init_db()
    server.vault_add("26-9999 (T)", "generated", "test", b"pdf-bytes-1")
    server.vault_add("26-9999 (T)", "signed_member", "test", b"pdf-bytes-2")
    out = server.ledger_verify()
    assert out["chain_intact"] and out["versions_checked"] == 2


def test_next_ctrl_continues_after_existing(tmp_path, monkeypatch):
    import server
    monkeypatch.setattr(server, "DB_PATH", tmp_path / "test.db")
    server.init_db()
    assert server._next_ctrl() == "26-1 (E2096)"
    server.vault_add("26-41 (E2096)", "generated", "test", b"x")
    assert server._next_ctrl() == "26-42 (E2096)"

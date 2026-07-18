"""e2096 platform server (demonstration prototype).

Pipeline: chat -> retrieval (corpus.json) -> action derivation (MockLLM)
-> fill the real DAF Form 2096 -> member concurrence signature -> per-role
incremental form-fill + PAdES digital signature (demo PKI) -> every signature
event vaulted as a full PDF version in a hash-chained, write-once SQLite
ledger -> final flatten + archive. Cases persist across restarts.

Run (HTTP):  uvicorn server:app --port 8096 --app-dir e2096-platform
Run (mTLS/CAC-style):  see CAC_SIGNING.md
"""
import hashlib
import json
import re
import sqlite3
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import formfill
import pdf_engine
import signing
from retrieval import Retriever
from scenarios import MEMBER, MEMBERS, ROUTING, MockLLM

ROOT = Path(__file__).parent
ARCHIVE_DIR = ROOT / "archive"
DB_PATH = ROOT / "e2096.db"

F = "topmostSubform[0].Page1[0].{}[0]".format

app = FastAPI(title="e2096 (demo prototype)")
retriever = Retriever(ROOT / "corpus.json")
llm = MockLLM()
signing.ensure_pki()


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS archive (
            ctrl_no TEXT PRIMARY KEY,
            member TEXT NOT NULL,
            action TEXT NOT NULL,
            effective_date TEXT NOT NULL,
            finalized_utc TEXT NOT NULL,
            approvals TEXT NOT NULL,
            pdf_file TEXT NOT NULL
        );
        CREATE TRIGGER IF NOT EXISTS archive_no_update BEFORE UPDATE ON archive
        BEGIN SELECT RAISE(ABORT, 'archive records are read-only'); END;
        CREATE TRIGGER IF NOT EXISTS archive_no_delete BEFORE DELETE ON archive
        BEGIN SELECT RAISE(ABORT, 'archive records are read-only'); END;

        -- the vault: one full PDF per signature event, hash-chained
        CREATE TABLE IF NOT EXISTS versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ctrl_no TEXT NOT NULL,
            version INTEGER NOT NULL,
            event TEXT NOT NULL,
            actor TEXT NOT NULL,
            utc TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            prev_sha256 TEXT,
            pdf BLOB NOT NULL,
            UNIQUE (ctrl_no, version)
        );
        CREATE TRIGGER IF NOT EXISTS versions_no_update BEFORE UPDATE ON versions
        BEGIN SELECT RAISE(ABORT, 'version records are read-only'); END;
        CREATE TRIGGER IF NOT EXISTS versions_no_delete BEFORE DELETE ON versions
        BEGIN SELECT RAISE(ABORT, 'version records are read-only'); END;

        -- working set: in-flight cases (mutable; survives restarts)
        CREATE TABLE IF NOT EXISTS cases (
            id TEXT PRIMARY KEY,
            ctrl_no TEXT UNIQUE NOT NULL,
            label TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            step INTEGER NOT NULL DEFAULT 0,
            created TEXT NOT NULL,
            updated TEXT NOT NULL,
            fields_json TEXT NOT NULL,
            pdf BLOB NOT NULL
        );
        """
    )
    try:  # additive migration: member profile per case
        conn.execute("ALTER TABLE cases ADD COLUMN member_json TEXT")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()


init_db()


def _member_cn(m: dict) -> str:
    stem = re.sub(r"[^A-Z]+", ".", m["name"].upper()).strip(".")
    return f"{stem}.E2096DEMO.{m['dodid']}"


def _member_key(m: dict) -> str:
    return f"member_{m['id']}"


def _next_ctrl() -> str:
    """Highest control number on record + 1 — stable across restarts."""
    conn = db()
    nums = [0]
    for table in ("cases", "versions"):
        for (ctrl,) in conn.execute(f"SELECT DISTINCT ctrl_no FROM {table}"):
            m = re.match(r"26-(\d+)", ctrl)
            if m:
                nums.append(int(m.group(1)))
    conn.close()
    return f"26-{max(nums) + 1} (E2096)"


def vault_add(ctrl_no: str, event: str, actor: str, pdf_bytes: bytes) -> dict:
    """Append a full PDF snapshot to the write-once, hash-chained vault."""
    sha = hashlib.sha256(pdf_bytes).hexdigest()
    conn = db()
    prev = conn.execute(
        "SELECT sha256, version FROM versions WHERE ctrl_no=? ORDER BY version DESC LIMIT 1",
        (ctrl_no,)).fetchone()
    version = (prev["version"] + 1) if prev else 0
    conn.execute(
        "INSERT INTO versions (ctrl_no, version, event, actor, utc, sha256, prev_sha256, pdf)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (ctrl_no, version, event, actor,
         datetime.now(timezone.utc).isoformat(), sha,
         prev["sha256"] if prev else None, pdf_bytes))
    conn.commit()
    conn.close()
    return {"version": version, "sha256": sha}


def case_get(case_id: str) -> dict:
    conn = db()
    row = conn.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "case not found")
    case = dict(row)
    case["pdf"] = bytes(case["pdf"])
    case["fields"] = json.loads(case["fields_json"])
    case["member"] = json.loads(case["member_json"]) if case.get("member_json") else MEMBER
    return case


def case_update(case_id: str, **cols) -> None:
    cols["updated"] = datetime.now(timezone.utc).isoformat()
    sets = ", ".join(f"{k}=?" for k in cols)
    conn = db()
    conn.execute(f"UPDATE cases SET {sets} WHERE id=?", (*cols.values(), case_id))
    conn.commit()
    conn.close()


class AskBody(BaseModel):
    message: str
    member_id: str = "snuffy"


class ApproveBody(BaseModel):
    role: str


class RejectBody(BaseModel):
    role: str
    comment: str = ""


@app.get("/api/profile")
def profile():
    return {"member": MEMBER, "members": MEMBERS, "routing": ROUTING, "llm": llm.name}


@app.get("/api/whoami")
def whoami(request: Request):
    """CAC identity via mutual TLS (see CAC_SIGNING.md)."""
    tls = request.scope.get("extensions", {}).get("tls", {})
    cert_name = tls.get("client_cert_name")
    if cert_name:
        return {"authenticated": True, "method": "mTLS client certificate",
                "subject": cert_name}
    return {"authenticated": False, "method": "none (HTTP demo mode)",
            "subject": MEMBER["name"] + " (assumed demo identity)"}


@app.post("/api/ask")
def ask(body: AskBody):
    member = MEMBERS.get(body.member_id, MEMBER)
    # member context steers retrieval toward the right career-field entries
    query = f"{body.message} {member['cafsc'][:3]} {member['career_field']}"
    retrieved = retriever.retrieve(query, k=3)
    derived = llm.derive(body.message, retrieved, member)
    if derived is None:
        return {"reply": ("I can draft 2096s for skill-level upgrades, SEI awards, "
                          "duty title changes, retraining, and SDAP. Describe what "
                          "happened, or use a quick action."),
                "retrieved": retrieved, "case": None}
    case_id = uuid.uuid4().hex[:12]
    ctrl = _next_ctrl()
    fields = dict(derived["fields"])
    fields[F("PERSONNEL_ACTION_NO_C")] = ctrl
    remarks = fields.pop(F("V_REMARKSRow1"), None)

    pdf = pdf_engine.render_2096(fields, {})
    if remarks:  # multi-line appearance via our own AP generator
        pdf = formfill.fill_incremental(pdf, {F("V_REMARKSRow1"): remarks})
        fields[F("V_REMARKSRow1")] = remarks

    now = datetime.now(timezone.utc).isoformat()
    conn = db()
    conn.execute(
        "INSERT INTO cases (id, ctrl_no, label, status, step, created, updated,"
        " fields_json, pdf, member_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (case_id, ctrl, derived["label"], "active", 0, now, now,
         json.dumps(fields), pdf, json.dumps(member)))
    conn.commit()
    conn.close()
    vault_add(ctrl, "generated", f"e2096 LLM pipeline ({llm.name})", pdf)
    return {"reply": derived["reply"], "retrieved": retrieved,
            "case": _summary(case_get(case_id))}


def _summary(case: dict) -> dict:
    member = case.get("member", MEMBER)
    return {
        "id": case["id"], "ctrl": case["ctrl_no"], "label": case["label"],
        "step": case["step"], "status": case["status"],
        "member": {"id": member["id"], "name": member["name"], "grade": member["grade"]},
        "routing": [
            {**r,
             "name": f"{member['name']}, {member['grade']}" if r["role"] == "member" else r["name"],
             "status": (
                "rejected" if case["status"] == "rejected" and i == case["step"] else
                "approved" if i < case["step"] else
                "pending" if (i == case["step"] and case["status"] == "active") else
                "queued")}
            for i, r in enumerate(ROUTING)],
        "finalized": case["status"] == "finalized",
        "rejected": case["status"] == "rejected",
    }


@app.get("/api/cases")
def list_cases():
    """The unit action queue: everything in flight plus recent outcomes."""
    conn = db()
    rows = conn.execute(
        "SELECT id, ctrl_no, label, status, step, created, updated FROM cases"
        " ORDER BY updated DESC").fetchall()
    conn.close()
    out = []
    for r in rows:
        pending = (ROUTING[r["step"]]["label"]
                   if r["status"] == "active" and r["step"] < len(ROUTING) else None)
        out.append({**dict(r), "pending": pending})
    return {"cases": out}


@app.get("/api/case/{case_id}")
def get_case(case_id: str):
    return _summary(case_get(case_id))


@app.get("/api/case/{case_id}/preview.png")
def case_preview(case_id: str):
    png = pdf_engine.render_preview_png(case_get(case_id)["pdf"])
    return Response(png, media_type="image/png",
                    headers={"Cache-Control": "no-store"})


@app.get("/api/case/{case_id}/pdf")
def case_pdf(case_id: str):
    """Current state. Unsigned signature blocks remain live /Sig fields —
    signable in Adobe/DoD eSign with a real CAC."""
    case = case_get(case_id)
    return Response(case["pdf"], media_type="application/pdf", headers={
        "Content-Disposition": f'inline; filename="{case["ctrl_no"].split(" ")[0]}_draft.pdf"'})


ROLE_FIELDS = {
    "member": {
        "text": lambda today: {F("Date18_af_date"): today},
        "checkbox": {F("Check_Box2"): "/Yes"},  # DO concur
        "sig_field": F("SIGNATURE_OF_MEMBER"),
        "reason": "Member concurrence: CONCUR (DEMO)",
        "certify": True,  # author signature; permits later form-fill + sigs
    },
    "supervisor": {
        "text": lambda today: {
            F("Date17_af_date"): today,
            F("NAME_GRADE_TITLE_DUTY_PHONE_SUPERVISORREQUESTING_OFFICIAL"):
                "DOE, TAYLOR R., TSgt, NCOIC Cyber Defense, 555-0101 (DEMO)"},
        "checkbox": {},
        "sig_field": F("Signature8"),
        "reason": "Supervisor certification of classification action (DEMO)",
        "certify": False,
    },
    "commander": {
        "text": lambda today: {
            F("Date7_af_date"): today,
            F("TYPED_NAME_GRADE_AND_TITLE"): "SMITH, RILEY K., Lt Col, Commander (DEMO)"},
        "checkbox": {},
        "sig_field": F("Signature9"),
        "reason": "Commander approval (DEMO)",
        "certify": False,
    },
    "fss": {
        "text": lambda today: {
            F("Date6_af_date"): today,
            F("TYPED_NAME_GRADE_AND_TITLE_2"): "LEE, KAI M., SrA, Personnel Specialist (DEMO)",
            F("Headquarters"): "52 FSS (SIMULATED)"},
        "checkbox": {F("Check_Box4"): "/Yes"},
        "sig_field": F("Signature12"),
        "reason": "Personnel official action: APPROVED (DEMO)",
        "certify": False,
    },
}


def _gate(case: dict, role: str) -> dict:
    if case["status"] != "active":
        raise HTTPException(409, f"case is {case['status']}")
    expected = ROUTING[case["step"]]
    if role != expected["role"]:
        raise HTTPException(409, f"next actor is {expected['role']}")
    return expected


@app.post("/api/case/{case_id}/approve")
def approve(case_id: str, body: ApproveBody):
    case = case_get(case_id)
    expected = _gate(case, body.role)

    today = date.today().isoformat()
    spec = ROLE_FIELDS[expected["role"]]
    if expected["role"] == "member":
        signer_key, actor = _member_key(case["member"]), _member_cn(case["member"])
        signing.ensure_role(signer_key, actor)
    else:
        signer_key, actor = expected["role"], signing.ROLE_CNS[expected["role"]]
    pdf = formfill.fill_incremental(case["pdf"], spec["text"](today), spec["checkbox"])
    pdf = signing.seal(pdf, spec["sig_field"], signer_key,
                       spec["reason"], certify=spec["certify"])
    step = case["step"] + 1
    finalized = step >= len(ROUTING)
    case_update(case_id, pdf=pdf, step=step,
                status="finalized" if finalized else "active")
    ver = vault_add(case["ctrl_no"], f"signed_{expected['role']}", actor, pdf)
    case = case_get(case_id)
    if finalized:
        _finalize(case)
    return {"case": _summary(case), "finalized": finalized, "vaulted": ver,
            "signatures": signing.validate(pdf)}


@app.post("/api/case/{case_id}/reject")
def reject(case_id: str, body: RejectBody):
    """Disapproval: gatekeeping means the ability to say no. The rejected
    state is vaulted too — the paper trail includes the 'no'."""
    case = case_get(case_id)
    expected = _gate(case, body.role)
    comment = (body.comment or "no comment provided").strip()[:200]
    actor = (_member_cn(case["member"]) if expected["role"] == "member"
             else signing.ROLE_CNS[expected["role"]])
    case_update(case_id, status="rejected")
    ver = vault_add(case["ctrl_no"],
                    f"rejected_{expected['role']}: {comment}", actor, case["pdf"])
    return {"case": _summary(case_get(case_id)), "vaulted": ver}


def _finalize(case: dict) -> None:
    flat = pdf_engine.flatten(case["pdf"])
    fname = f"{case['ctrl_no'].split(' ')[0].replace('-', '_')}_{case['id']}.pdf"
    (ARCHIVE_DIR / fname).write_bytes(flat)
    vault_add(case["ctrl_no"], "flattened_archive", "e2096 platform", flat)
    conn = db()
    conn.execute(
        "INSERT INTO archive VALUES (?,?,?,?,?,?,?)",
        (case["ctrl_no"], case["member"]["name"], case["label"], date.today().isoformat(),
         datetime.now(timezone.utc).isoformat(),
         ", ".join(f"{r['label']}: {signing.ROLE_CNS[r['role']]}" for r in ROUTING),
         fname))
    conn.commit()
    conn.close()


@app.get("/api/case/{case_id}/packet")
def data_packet(case_id: str):
    """The swivel-chair killer: the validated, structured dataset the FSS
    personnelist gets instead of re-keying the form into MilPDS."""
    case = case_get(case_id)
    prefix = "topmostSubform[0].Page1[0]."
    fields = {k[len(prefix):-3] if k.startswith(prefix) else k: v
              for k, v in case["fields"].items() if v}
    conn = db()
    events = [dict(r) for r in conn.execute(
        "SELECT version, event, actor, utc, sha256 FROM versions WHERE ctrl_no=?"
        " ORDER BY version", (case["ctrl_no"],))]
    conn.close()
    return {
        "schema": "e2096.milpds-staging.v1 (DEMO)",
        "control_number": case["ctrl_no"],
        "status": case["status"],
        "action": case["label"],
        "member": case["member"],
        "form_fields": fields,
        "signature_events": events,
        "note": "DEMO — fictional data. In production this packet feeds the "
                "FSS action queue and, long-term, the MilPDS API layer.",
    }


SIG_ORDER = [F("SIGNATURE_OF_MEMBER"), F("Signature8"), F("Signature9"), F("Signature12")]


@app.post("/api/case/{case_id}/ingest")
def ingest(case_id: str, file: UploadFile = File(...)):
    """Re-ingest an exported PDF that was signed externally (e.g. CAC-signed in
    Adobe/DoD eSign). Signatures are validated against the trust root and the
    control number must match before the vault accepts it.

    Kept sync deliberately: pyHanko's validator drives its own asyncio loop,
    which cannot run inside FastAPI's event loop thread."""
    case = case_get(case_id)
    if case["status"] != "active":
        raise HTTPException(409, f"case is {case['status']}")
    data = file.file.read()
    if len(data) > 10_000_000 or not data.startswith(b"%PDF"):
        raise HTTPException(400, "not a PDF (or too large)")
    try:
        sigs = signing.validate(data)
    except Exception as e:
        raise HTTPException(400, f"could not parse signatures: {e}")
    if not sigs:
        raise HTTPException(400, "no digital signatures found")
    bad = [s["field"] for s in sigs if not (s["intact"] and s["valid"] and s["trusted"])]
    if bad:
        raise HTTPException(400, f"signature(s) failed validation: {bad}")

    from pypdf import PdfReader
    import io as _io
    flds = PdfReader(_io.BytesIO(data)).get_fields() or {}
    ctrl_val = (flds.get(F("PERSONNEL_ACTION_NO_C")) or {}).get("/V", "")
    if str(ctrl_val).strip() != case["ctrl_no"]:
        raise HTTPException(400,
            f"control number mismatch: form says {ctrl_val!r}, case is {case['ctrl_no']!r}")

    signed = {s["field"] for s in sigs}
    step = 0
    for f_name in SIG_ORDER:  # signatures must accrue in routing order
        if f_name in signed:
            step += 1
        else:
            break
    if step < case["step"]:
        raise HTTPException(409, "uploaded document has fewer signatures than the case")

    finalized = step >= len(ROUTING)
    case_update(case_id, pdf=data, step=step,
                status="finalized" if finalized else "active")
    ver = vault_add(case["ctrl_no"], "ingested_external",
                    f"upload validated: {len(sigs)} signature(s)", data)
    case = case_get(case_id)
    if finalized:
        _finalize(case)
    return {"case": _summary(case), "finalized": finalized, "vaulted": ver,
            "signatures": sigs}


@app.get("/api/case/{case_id}/signatures")
def case_signatures(case_id: str):
    return {"signatures": signing.validate(case_get(case_id)["pdf"])}


@app.get("/api/case/{case_id}/versions")
def case_versions(case_id: str):
    case = case_get(case_id)
    conn = db()
    rows = [dict(r) for r in conn.execute(
        "SELECT id, version, event, actor, utc, sha256, prev_sha256 FROM versions"
        " WHERE ctrl_no=? ORDER BY version", (case["ctrl_no"],))]
    conn.close()
    return {"ctrl": case["ctrl_no"], "versions": rows}


@app.get("/api/version/{version_id}/pdf")
def version_pdf(version_id: int):
    conn = db()
    row = conn.execute("SELECT ctrl_no, version, pdf FROM versions WHERE id=?",
                       (version_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404)
    name = f"{row['ctrl_no'].split(' ')[0]}_v{row['version']}.pdf"
    return Response(row["pdf"], media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{name}"'})


@app.get("/api/ledger/verify")
def ledger_verify():
    """Recompute every hash and check the chain — tamper-evidence proof."""
    conn = db()
    rows = conn.execute(
        "SELECT ctrl_no, version, sha256, prev_sha256, pdf FROM versions"
        " ORDER BY ctrl_no, version").fetchall()
    conn.close()
    checked, broken = 0, []
    prev_by_ctrl: dict[str, str] = {}
    for r in rows:
        checked += 1
        actual = hashlib.sha256(r["pdf"]).hexdigest()
        expect_prev = prev_by_ctrl.get(r["ctrl_no"])
        if actual != r["sha256"] or (r["version"] > 0 and r["prev_sha256"] != expect_prev):
            broken.append(f"{r['ctrl_no']} v{r['version']}")
        prev_by_ctrl[r["ctrl_no"]] = r["sha256"]
    return {"versions_checked": checked, "chain_intact": not broken, "broken": broken}


@app.get("/api/metrics")
def metrics():
    """The business case, live from the records."""
    conn = db()
    by_status = dict(conn.execute(
        "SELECT status, COUNT(*) FROM cases GROUP BY status").fetchall())
    # cycle time: generated (v0) -> flattened_archive, per finalized ctrl
    cycles = []
    for ctrl, in conn.execute(
            "SELECT DISTINCT ctrl_no FROM versions WHERE event='flattened_archive'"):
        t = conn.execute(
            "SELECT MIN(utc), MAX(utc) FROM versions WHERE ctrl_no=?", (ctrl,)).fetchone()
        try:
            dt = (datetime.fromisoformat(t[1]) - datetime.fromisoformat(t[0])).total_seconds()
            cycles.append(dt)
        except (TypeError, ValueError):
            pass
    n_versions = conn.execute("SELECT COUNT(*) FROM versions").fetchone()[0]
    conn.close()
    finalized = by_status.get("finalized", 0)
    cycles.sort()
    median = cycles[len(cycles) // 2] if cycles else None
    return {
        "cases_total": sum(by_status.values()),
        "by_status": by_status,
        "finalized": finalized,
        "vault_versions": n_versions,
        "man_hours_saved": round(finalized * 2.4, 1),
        # median: robust to wall-clock idle between demo sessions
        "median_cycle_seconds": round(median, 1) if median is not None else None,
        "assumption": "2.5 hrs manual routing vs ~5 min digital per finalized action",
    }


@app.get("/api/archive")
def list_archive():
    conn = db()
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM archive ORDER BY finalized_utc DESC")]
    conn.close()
    return {"records": rows}


@app.get("/api/archive/{ctrl_no}/pdf")
def archive_pdf(ctrl_no: str):
    conn = db()
    row = conn.execute(
        "SELECT pdf_file FROM archive WHERE ctrl_no = ?", (ctrl_no,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404)
    return FileResponse(ARCHIVE_DIR / row["pdf_file"], media_type="application/pdf")


@app.get("/report", response_class=HTMLResponse)
def report():
    """Printable one-pager: the business case, live from the records."""
    m = metrics()
    conn = db()
    arch = [dict(r) for r in conn.execute(
        "SELECT ctrl_no, member, action, finalized_utc FROM archive"
        " ORDER BY finalized_utc DESC LIMIT 10")]
    conn.close()
    ledger = ledger_verify()
    rows = "".join(
        f"<tr><td>{a['ctrl_no']}</td><td>{a['member']}</td><td>{a['action']}</td>"
        f"<td>{a['finalized_utc'][:16].replace('T', ' ')}Z</td></tr>" for a in arch)
    cyc = m["median_cycle_seconds"]
    cyc_txt = "—" if cyc is None else (f"{cyc:.0f} seconds" if cyc < 120 else f"{cyc/60:.0f} minutes")
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>e2096 — SBIR Demonstration Report</title>
<style>body{{font:13px/1.55 Segoe UI,sans-serif;color:#1a1d24;max-width:820px;margin:24px auto;padding:0 16px}}
h1{{font-size:22px;color:#1b2a4a;margin-bottom:2px}} h2{{font-size:15px;color:#1b2a4a;margin:18px 0 6px;border-bottom:2px solid #c9a227;padding-bottom:2px}}
.warn{{background:#b3542f;color:#fff;font-weight:700;text-align:center;padding:4px;font-size:11px}}
.kpi{{display:flex;gap:14px;margin:12px 0}} .kpi div{{flex:1;border:1px solid #d4dae4;border-radius:8px;padding:10px;text-align:center}}
.kpi b{{display:block;font-size:20px;color:#2f5fb3}} table{{width:100%;border-collapse:collapse;font-size:12px}}
td,th{{border-bottom:1px solid #d4dae4;padding:4px 8px;text-align:left}} th{{background:#e9edf5}}
@media print{{.noprint{{display:none}}}}</style></head><body>
<div class="warn">DEMONSTRATION PROTOTYPE — FICTIONAL DATA — NOT AN OFFICIAL DoD SYSTEM</div>
<h1>e2096 — DAF Form 2096 Automation</h1>
<div style="color:#667">Demonstration report generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%MZ')} from live platform records</div>
<h2>The problem</h2>
<p>~200,000 Form 2096 actions route across the Air Force annually as hand-typed PDFs chased
through email — roughly 2.5 hours of collective labor each, with FSS personnelists re-keying
every approved form into MilPDS by hand.</p>
<h2>Demonstrated results (this instance)</h2>
<div class="kpi">
<div><b>{m['cases_total']}</b>cases processed</div>
<div><b>{m['finalized']}</b>finalized w/ full signature chains</div>
<div><b>{cyc_txt}</b>median signature-chain cycle</div>
<div><b>{m['man_hours_saved']}</b>man-hours saved (at 2.4 hrs/action)</div>
<div><b>{m['vault_versions']}</b>vaulted PDF versions, chain {'INTACT' if ledger['chain_intact'] else 'BROKEN'}</div>
</div>
<h2>What the prototype proves</h2>
<ul>
<li>Plain-English request → AFECD-grounded retrieval → the <b>official DAF Form 2096 PDF</b> auto-populated field-by-field.</li>
<li>Four-step human gatekeeping (Member → Supervisor → Commander → FSS), each applying a <b>real PAdES digital signature</b> into the form's signature fields; all signatures coexist and validate.</li>
<li>Every signature event stores the complete PDF in a <b>write-once, SHA-256 hash-chained vault</b> — recomputed live for this report.</li>
<li>Disapprovals are vaulted with comments; in-flight cases persist across outages; FSS receives a structured <b>MilPDS-ready data packet</b> instead of re-keying.</li>
</ul>
<h2>Scaled impact (AF-wide)</h2>
<p>200,000 actions × ~2.4 hours reclaimed ≈ <b>480,000 man-hours per year</b> returned to
primary mission duties, plus elimination of lost forms and transcription errors.</p>
<h2>Recent finalized actions</h2>
<table><tr><th>Ctrl #</th><th>Member</th><th>Action</th><th>Finalized</th></tr>{rows}</table>
<p class="noprint" style="margin-top:14px"><a href="/">← back to platform</a> · window.print() for PDF</p>
</body></html>"""


@app.middleware("http")
async def no_html_cache(request: Request, call_next):
    """Demo machines must never show a stale UI after an upgrade."""
    resp = await call_next(request)
    if "text/html" in resp.headers.get("content-type", ""):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


app.mount("/", StaticFiles(directory=ROOT / "static", html=True), name="static")

"""Action derivation layer ("the LLM").

MockLLM: deterministic keyword classifier + templates, so the demo runs offline
and never hallucinates mid-brief.

GenAIMilAdapter: drop-in stub showing where the GenAI.mil-hosted model plugs in.
The contract is identical: (member, message, retrieved_passages) ->
{reply, action_label, fields} where `fields` are real DAF 2096 AcroForm values.
"""
from datetime import date

# All member data is fictional demo data.
MEMBERS = {
    "snuffy": {
        "id": "snuffy",
        "name": "SNUFFY, JORDAN A.", "grade": "SSgt",
        "ssn": "000-00-0000 (DEMO)", "dodid": "1234567890",
        "unit": "52 CS (DEMO)", "pas": "TE1CFQJW",
        "cafsc": "1D751B", "dafsc": "1D751B",
        "duty_title": "Cyber Defense Technician",
        "office_symbol": "SCO", "duty_phone": "555-0100",
        "to_org": "52 FSS/FSM (SIMULATED)", "from_org": "52 CS/SCO (SIMULATED)",
        "career_field": "Cyber Defense Operations",
    },
    "garcia": {
        "id": "garcia",
        "name": "GARCIA, ALEX B.", "grade": "SrA",
        "ssn": "000-00-0000 (DEMO)", "dodid": "2345678901",
        "unit": "52 MXS (DEMO)", "pas": "TE1CFQJW",
        "cafsc": "2A353", "dafsc": "2A353",
        "duty_title": "Tactical Aircraft Maintenance Journeyman",
        "office_symbol": "MXA", "duty_phone": "555-0200",
        "to_org": "52 FSS/FSM (SIMULATED)", "from_org": "52 MXS/MXA (SIMULATED)",
        "career_field": "Tactical Aircraft Maintenance",
    },
    "okafor": {
        "id": "okafor",
        "name": "OKAFOR, SAM C.", "grade": "TSgt",
        "ssn": "000-00-0000 (DEMO)", "dodid": "3456789012",
        "unit": "52 MDG (DEMO)", "pas": "TE1CFQJW",
        "cafsc": "4N051", "dafsc": "4N051",
        "duty_title": "Aerospace Medical Technician",
        "office_symbol": "SGN", "duty_phone": "555-0300",
        "to_org": "52 FSS/FSM (SIMULATED)", "from_org": "52 MDG/SGN (SIMULATED)",
        "career_field": "Aerospace Medical Service",
    },
}
MEMBER = MEMBERS["snuffy"]  # default profile

ROUTING = [
    {"role": "member", "label": "Member Concurrence", "name": "(member)"},
    {"role": "supervisor", "label": "Supervisor", "name": "DOE, TAYLOR R., TSgt, NCOIC, 555-0101"},
    {"role": "commander", "label": "Commander", "name": "SMITH, RILEY K., Lt Col, Commander"},
    {"role": "fss", "label": "FSS Personnel Official", "name": "LEE, KAI M., SrA, Personnel Specialist"},
]

F = "topmostSubform[0].Page1[0].{}[0]"


def _f(name):
    return F.format(name)


def bump_skill(afsc: str, to: str = "7") -> str:
    """1D751B -> 1D771B, 2A353 -> 2A373 (skill level is the 4th character)."""
    return afsc[:3] + to + afsc[4:]


def _base_fields(m: dict) -> dict:
    return {
        _f("Text1"): m["to_org"],
        _f("FROM_OrganizationOffice_Symbol"): m["from_org"],
        _f("NAME_Last_First_MI"): m["name"],
        _f("GRADE"): m["grade"],
        _f("SSN"): m["ssn"],
        _f("UNIT"): m["unit"],
        # Section III concurrence is the member's own signature step
    }


def scenarios(today: str, m: dict = MEMBER) -> dict:
    b = _base_fields(m)
    up = bump_skill(m["cafsc"])
    return {
        "upgrade": {
            "label": f"Award 7-skill level ({m['cafsc']} → {up})",
            "keywords": ("7-level", "seven", "upgrade", "craftsman", "ugt", "skill level"),
            "reply": (
                f"You meet the sample-AFECD requirements for {up}: UGT complete, 12+ months "
                f"in the 5-level, grade, and craftsman CDCs done.\n\nI drafted the 2096: Award "
                f"AFSC {up} as Craftsman, CAFSC redesignated {m['cafsc']} → {up}, effective "
                "today, with the citation in Remarks. Your concurrence signature is first."
            ),
            "fields": {
                **b,
                _f("AWARD_AFSC"): up,
                _f("AS"): "Craftsman (7-lvl)",
                _f("Date1_af_date"): today,
                _f("REDESIGNATE_3"): m["cafsc"],
                _f("TO_2"): up,
                _f("Date3_af_date"): today,
                _f("COMPLETED_AFSC"): up,
                _f("TS_CODE_3"): "T",
                _f("V_REMARKSRow1"): (
                    f"7-skill level UGT complete; AFECD {m['career_field']} craftsman "
                    "prerequisites verified against training record (UGT, 12 mo in 5-level, "
                    "grade, CDC set). Authority: AFECD/DAFMAN 36-2689 (sample citations). "
                    "DEMO — fictional data."
                ),
            },
        },
        "sei": {
            "label": "Designate SEI 8B0 (Instructor)",
            "keywords": ("sei", "instructor", "identifier", "special experience"),
            "reply": (
                "Your record shows the instructor methodology course complete and 6+ months "
                "instructing — that satisfies the sample-AFECD criteria for SEI 8B0.\n\n"
                f"I drafted the 2096: Designate SEI 8B0 with CAFSC {m['cafsc']}."
            ),
            "fields": {
                **b,
                _f("DESIGNATE_CAFSC_SEI"): "8B0",
                _f("DESIGNATE_SEI"): "8B0",
                _f("SEI_1"): "C",
                _f("AFSC_2"): m["cafsc"],
                _f("V_REMARKSRow1"): (
                    "SEI 8B0 requested: formal instructor course complete plus minimum 6 months "
                    "instructing in awarded AFSC, per AFECD attachment criteria (sample). "
                    "DEMO — fictional data."
                ),
            },
        },
        "duty": {
            "label": f"Duty title change — {m['career_field']} Flight Chief",
            "keywords": ("duty title", "title", "flight chief", "position"),
            "reply": (
                "UMD position 0043 (Flight Chief) matches an authorized craftsman-NCO title "
                "per the sample AFECD standard.\n\nI drafted the 2096 Duty Information block: "
                f"office symbol {m['office_symbol']}, position 0043, effective the date you "
                "assumed the position."
            ),
            "fields": {
                **b,
                _f("DAFSC"): m["dafsc"],
                _f("Date9_af_date"): today,
                _f("OFFICE_SYMBOL"): m["office_symbol"],
                _f("DUTY_PH"): m["duty_phone"],
                _f("POSITION_NO"): "0043",
                _f("OSC"): m["office_symbol"],
                _f("COMD_LVL"): "SQ",
                _f("DUTY_TITLE"): "Flight Chief",
                _f("Text20"): "AFECD Duty Title Standards / UMD position 0043 (sample authority)",
                _f("V_REMARKSRow1"): (
                    "Duty title aligned to UMD position 0043, Flight Chief. Effective date is "
                    "date member assumed position. DEMO — fictional data."
                ),
            },
        },
        "retrain": {
            "label": f"Retraining — CAFSC to 1D731Z, withdraw PAFSC {m['cafsc']}",
            "keywords": ("retrain", "retraining", "cross-train", "1d7x1z"),
            "reply": (
                "Approved retraining into 1D7X1Z means you enter at the 3-skill level (1D731Z) "
                "while in UGT, and your current primary AFSC is withdrawn — the sample AFECD "
                "requires both actions together.\n\nI drafted the 2096 with the CAFSC change, "
                "PAFSC withdrawal, and date initially entered retraining — the pairing FSS "
                "usually has to catch manually."
            ),
            "fields": {
                **b,
                _f("REDESIGNATE_3"): m["cafsc"],
                _f("TO_2"): "1D731Z",
                _f("Date3_af_date"): today,
                _f("WITHDRAW_AFSC_2"): f"{m['cafsc']} (PAFSC)",
                _f("Date5_af_date"): today,
                _f("V_REMARKSRow1"): (
                    f"Approved retraining action: simultaneous CAFSC change to 1D731Z and "
                    f"withdrawal of PAFSC {m['cafsc']} per AFECD retraining procedures (sample). "
                    "TS eligibility on file. DEMO — fictional data."
                ),
            },
        },
        "sdap": {
            "label": "Assign proficiency pay (SDAP SD-3)",
            "keywords": ("sdap", "proficiency", "special duty pay", "pay"),
            "reply": (
                "Your position is designated SD-3 in the sample SDAP table, which carries "
                "$225/month.\n\nI drafted the 2096 Assign Proficiency Pay block: SS rating "
                f"SD-3, $225, AFSC {m['cafsc']}, effective today."
            ),
            "fields": {
                **b,
                _f("SS_RATING"): "SD-3",
                _f("AMOUNT"): "225",
                _f("AFSC"): m["cafsc"],
                _f("Date8_af_date"): today,
                _f("V_REMARKSRow1"): (
                    "SDAP start action: member assigned to designated special duty position, SD "
                    "rating 3, $225/month per DAFI 36-3012 (sample citation). DEMO — fictional data."
                ),
            },
        },
    }


class MockLLM:
    """Deterministic stand-in for the GenAI.mil-hosted model."""

    name = "MockLLM (offline, deterministic)"

    def derive(self, message: str, retrieved: list[dict], member: dict = MEMBER) -> dict | None:
        today = date.today().isoformat()
        text = message.lower()
        # most-specific first so e.g. "special duty pay" doesn't match duty-title
        order = ("sdap", "retrain", "sei", "upgrade", "duty")
        scs = scenarios(today, member)
        for key in order:
            sc = scs[key]
            if any(k in text for k in sc["keywords"]):
                return {"key": key, "label": sc["label"], "reply": sc["reply"],
                        "fields": sc["fields"]}
        return None


class GenAIMilAdapter:
    """Where the real model plugs in. Same contract as MockLLM.derive().

    Production sketch:
      POST {GENAI_MIL_ENDPOINT}/v1/chat/completions  (CAC-authenticated, IL4/IL5)
      system prompt = 2096 field schema + retrieved AFECD passages
      response_format = structured JSON matching the `fields` dict of AcroForm values
    """

    def __init__(self, endpoint: str, api_key: str):
        self.endpoint, self.api_key = endpoint, api_key

    def derive(self, message: str, retrieved: list[dict], member: dict = MEMBER) -> dict | None:
        raise NotImplementedError("Requires GenAI.mil access from an accredited environment")

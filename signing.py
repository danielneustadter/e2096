"""Demo PKI and cryptographic sealing of DAF 2096 signature fields.

Reality check on CAC signing: the CAC private key never leaves the card, and
browser JavaScript cannot reach it. The accredited patterns are:
  (a) the member exports the still-signable PDF and signs it locally in
      Adobe/DoD eSign with the CAC (this platform's exports support that), or
  (b) the browser authenticates via mutual TLS (CAC + ActivClient presents the
      client cert), and the platform applies a server-side PAdES seal with an
      NPE certificate, recording the authenticated intent-to-sign.
This module implements (b) with a DEMO root CA standing in for DoD PKI.
Everything issued here is marked E2096 DEMO and trusts nothing outside itself.
"""
import datetime
import io
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from pyhanko import stamp
from pyhanko.keys import load_cert_from_pemder
from pyhanko.pdf_utils import layout
from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
from pyhanko.pdf_utils.reader import PdfFileReader
from pyhanko.sign import fields, signers
from pyhanko.sign.validation import validate_pdf_signature
from pyhanko_certvalidator import ValidationContext

PKI_DIR = Path(__file__).parent / "pki"
PASS = b"e2096demo"

# fictional signers, CN styled after DoD PKI but clearly marked DEMO
ROLE_CNS = {
    "member": "SNUFFY.JORDAN.A.E2096DEMO.1234567890",
    "supervisor": "DOE.TAYLOR.R.E2096DEMO.1000000001",
    "commander": "SMITH.RILEY.K.E2096DEMO.1000000002",
    "fss": "LEE.KAI.M.E2096DEMO.1000000003",
}


def _name(cn: str) -> x509.Name:
    return x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME, "US"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "E2096 DEMO (NOT DoD)"),
        x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, "DEMO PKI"),
        x509.NameAttribute(NameOID.COMMON_NAME, cn),
    ])


def ensure_pki() -> None:
    """Create demo CA once; issue any missing role/TLS certs against it."""
    PKI_DIR.mkdir(exist_ok=True)
    now = datetime.datetime.now(datetime.timezone.utc)

    if (PKI_DIR / "ca.pem").exists():
        ca_cert = x509.load_pem_x509_certificate((PKI_DIR / "ca.pem").read_bytes())
        ca_key = serialization.load_pem_private_key(
            (PKI_DIR / "ca.key").read_bytes(), password=None)
    else:
        ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        ca_cert = (
            x509.CertificateBuilder()
            .subject_name(_name("E2096 DEMO ROOT CA"))
            .issuer_name(_name("E2096 DEMO ROOT CA"))
            .public_key(ca_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now)
            .not_valid_after(now + datetime.timedelta(days=730))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True,
                                         crl_sign=True, content_commitment=False,
                                         key_encipherment=False, data_encipherment=False,
                                         key_agreement=False, encipher_only=False,
                                         decipher_only=False), critical=True)
            .sign(ca_key, hashes.SHA256())
        )
        (PKI_DIR / "ca.pem").write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
        (PKI_DIR / "ca.key").write_bytes(ca_key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()))

    for role, cn in ROLE_CNS.items():
        _write_p12(ca_key, ca_cert, role, cn)

    if not (PKI_DIR / "tls.crt").exists():
        key, cert = _issue_cert(ca_key, ca_cert, "localhost", server=True)
        (PKI_DIR / "tls.crt").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        (PKI_DIR / "tls.key").write_bytes(key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()))


def _issue_cert(ca_key, ca_cert, cn: str, server: bool = False):
    now = datetime.datetime.now(datetime.timezone.utc)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    b = (
        x509.CertificateBuilder()
        .subject_name(_name(cn))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=365))
        .add_extension(x509.KeyUsage(digital_signature=True,
                                     content_commitment=True,
                                     key_encipherment=server, data_encipherment=False,
                                     key_agreement=False, key_cert_sign=False,
                                     crl_sign=False, encipher_only=False,
                                     decipher_only=False), critical=True)
    )
    if server:
        b = b.add_extension(
            x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
    return key, b.sign(ca_key, hashes.SHA256())


def _write_p12(ca_key, ca_cert, key_name: str, cn: str) -> None:
    if (PKI_DIR / f"{key_name}.p12").exists():
        return
    key, cert = _issue_cert(ca_key, ca_cert, cn)
    (PKI_DIR / f"{key_name}.p12").write_bytes(
        serialization.pkcs12.serialize_key_and_certificates(
            key_name.encode(), key, cert, [ca_cert],
            serialization.BestAvailableEncryption(PASS)))


def ensure_role(key_name: str, cn: str) -> None:
    """Issue a signing cert for an arbitrary identity (e.g. per-member) once."""
    ensure_pki()
    if (PKI_DIR / f"{key_name}.p12").exists():
        return
    ca_cert = x509.load_pem_x509_certificate((PKI_DIR / "ca.pem").read_bytes())
    ca_key = serialization.load_pem_private_key(
        (PKI_DIR / "ca.key").read_bytes(), password=None)
    _write_p12(ca_key, ca_cert, key_name, cn)


_signers: dict[str, signers.SimpleSigner] = {}


def get_signer(role: str) -> signers.SimpleSigner:
    if role not in _signers:
        ensure_pki()
        _signers[role] = signers.SimpleSigner.load_pkcs12(
            PKI_DIR / f"{role}.p12", passphrase=PASS)
    return _signers[role]


def seal(pdf_bytes: bytes, sig_field: str, role: str, reason: str,
         certify: bool = False) -> bytes:
    """Apply a PAdES signature into an existing /Sig field (incremental update,
    preserving any earlier signatures). The first (supervisor) signature is a
    certification signature permitting subsequent form-fill + signatures."""
    w = IncrementalPdfFileWriter(io.BytesIO(pdf_bytes), strict=False)
    meta = signers.PdfSignatureMetadata(
        field_name=sig_field,
        reason=reason,
        location="e2096 platform (DEMONSTRATION)",
        certify=certify,
        docmdp_permissions=fields.MDPPerm.FILL_FORMS if certify else None,
    )
    # compact visible stamp that fits the form's small signature boxes
    style = stamp.TextStampStyle(
        stamp_text="Digitally signed by %(signer)s // %(ts)s // DEMO",
        border_width=0,
        inner_content_layout=layout.SimpleBoxLayoutRule(
            x_align=layout.AxisAlignment.ALIGN_MIN,
            y_align=layout.AxisAlignment.ALIGN_MID,
            margins=layout.Margins.uniform(1)),
    )
    pdf_signer = signers.PdfSigner(meta, signer=get_signer(role),
                                   stamp_style=style)
    out = pdf_signer.sign_pdf(w, existing_fields_only=True)
    return out.getvalue()


def validate(pdf_bytes: bytes) -> list[dict]:
    """Validate all embedded signatures against the demo CA."""
    ensure_pki()
    ca = load_cert_from_pemder(PKI_DIR / "ca.pem")
    vc = ValidationContext(trust_roots=[ca], allow_fetching=False)
    r = PdfFileReader(io.BytesIO(pdf_bytes), strict=False)
    results = []
    for sig in r.embedded_signatures:
        st = validate_pdf_signature(sig, vc)
        cn = sig.signer_cert.subject.native.get("common_name", "?") if sig.signer_cert else "?"
        results.append({
            "field": sig.field_name,
            "signer": cn,
            "intact": st.intact,
            "valid": st.valid,
            "trusted": st.trusted,
            "signed_at": st.signer_reported_dt.isoformat() if st.signer_reported_dt else None,
        })
    return results

# CAC signing and this demo

## What a browser can and cannot do with a CAC

The CAC's private key **never leaves the card**, and browser JavaScript has no
smartcard API. The two accredited patterns for "CAC signing" a web workflow:

1. **mTLS authentication + server-side seal** (what myFSS-class apps do, and
   what this demo implements). The browser, via CAC middleware (ActivClient /
   built-in Windows smart card support), presents the CAC certificate during
   the TLS handshake. The server verifies it against DoD PKI, records the
   authenticated identity + intent-to-sign, and applies a PAdES signature with
   a server (NPE) certificate. This demo's `signing.py` does exactly that with
   a self-contained DEMO CA in `pki/` standing in for DoD PKI.

2. **Local signing of the exported PDF.** Any version exported from the vault
   keeps its remaining signature blocks as live `/Sig` fields — open it in
   Adobe Acrobat (with CAC middleware) or DoD eSign and sign with the CAC
   directly. The platform can re-ingest the signed file in a future iteration.

## Running the demo in mTLS mode

Uvicorn does not expose the client certificate to the app; Hypercorn implements
the ASGI TLS extension, which `/api/whoami` reads:

```
cd e2096-platform
hypercorn server:app --bind localhost:8443 \
  --certfile pki/tls.crt --keyfile pki/tls.key \
  --ca-certs <client-ca-bundle.pem> --verify-mode CERT_OPTIONAL
```

- Demo without a CAC: use `pki/ca.pem` as the CA bundle and import a role
  `.p12` from `pki/` (passphrase `e2096demo`) into the OS certificate store —
  the browser will prompt for it and `/api/whoami` shows the subject.
- With a real CAC on a government machine: point `--ca-certs` at the DoD Root
  CA bundle (from cyber.mil/pki-pke) and the browser will prompt for the CAC
  certificate. **Do that only in an authorized environment** — this prototype
  is not accredited to process real PII; keep demo data fictional either way.

## Production notes for the Salesforce/myFSS build

- myFSS already fronts CAC/Okta authentication, so "who clicked Approve" is a
  solved problem there; the signature seal becomes a platform NPE certificate
  issued under DoD PKI, and DocMDP certification levels enforce what may change
  after each signature (this demo already sets FILL_FORMS on the first
  signature).
- The vault pattern (full PDF per signature event, SHA-256 hash-chained,
  write-once) maps to a Salesforce immutable content store or WORM S3 bucket;
  `/api/ledger/verify` shows the tamper-evidence check.

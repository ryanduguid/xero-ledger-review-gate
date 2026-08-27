from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")
DATA_FLOW = (ROOT / "DATA-FLOW.md").read_text(encoding="utf-8")


def normalise(text: str) -> str:
    return " ".join(text.casefold().split())


def test_readme_states_the_assurance_boundary_before_product_claims() -> None:
    opening = normalise(README[: README.index("```")])

    assert "synthetic-only" in opening
    assert "unkeyed" in opening
    assert "anyone who can replace the artefacts can replace the receipt" in opening


def test_public_docs_define_only_local_checksum_integrity() -> None:
    for document in (README, DATA_FLOW):
        text = normalise(document)
        assert "synthetic-only" in text
        assert "unkeyed" in text
        assert "local sha-256 checksum binding" in text
        assert "does not prove authorship, source system, origin, time, or immutability" in text


def test_retired_assurance_claims_do_not_return() -> None:
    public_docs = normalise(f"{README}\n{DATA_FLOW}")
    retired_claims = (
        "a validated xero tb export",
        "validated xero trial balance export",
        "cryptographic sha-256 sealing",
        "signed working paper",
        "receipt seals",
        "cryptographic evidence",
        "tamper-evident sha-256",
    )

    for claim in retired_claims:
        assert claim not in public_docs

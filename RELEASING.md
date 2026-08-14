# Releasing

Releases are built by GitHub Actions from an annotated tag on the exact `main` commit. Do not build or upload package assets by hand.

Before tagging:

1. Merge the release pull request and require every `main` check to pass.
2. Enable release immutability in repository settings. The workflow stops before publication while it is off.
3. Confirm `xero_ai_review_gateway/version.py` matches the `RELEASE_NOTES.md` heading and `uv lock --check` passes.
4. Create an annotated tag on current remote `main`, for example `git tag -a v0.1.1 -m "v0.1.1"` (or `-s` when signing is configured), then push only that tag.

The workflow runs the locked tests, builds the wheel and source distribution once, generates an SPDX 2.3 SBOM for the wheel and `SHA256SUMS`, records GitHub provenance and an SBOM attestation, then publishes the completed draft. An existing release is never overwritten.

Verify the downloaded release with:

```bash
gh release download v0.1.1 -R ryanduguid/xero-ai-review-gateway --dir release-v0.1.1
cd release-v0.1.1
sha256sum --check SHA256SUMS
gh attestation verify xero_ai_review_gateway-0.1.1-py3-none-any.whl -R ryanduguid/xero-ai-review-gateway
gh attestation verify xero_ai_review_gateway-0.1.1-py3-none-any.whl -R ryanduguid/xero-ai-review-gateway --predicate-type https://spdx.dev/Document/v2.3
gh release view v0.1.1 -R ryanduguid/xero-ai-review-gateway --json isImmutable
```

If any gate fails, inspect it before touching the tag or draft. Never move a published tag.

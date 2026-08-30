# Changelog

All notable changes to this project are documented here, following
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Initial public release.
- EMR Serverless provider: application lifecycle, submission, cost capture.
- Four-way comparison engine: correctness, functional, performance, cost.
- Access modes: plain Glue Data Catalog, Lake Formation full table access (FTA),
  Lake Formation fine-grained access control (FGAC).
- Self-contained HTML report with three-dropdown pair selection.
- `bootstrap` for empty accounts and tag-scoped `teardown`.
- Offline example over synthetic fixtures, no AWS account required.

### Added — Lake Formation data filters
- Row, column and cell data cell filters are created and granted for FGAC
  variants, and the harness asserts they were enforced. Disclosure beyond what a
  filter permits is a critical `FILTER_NOT_ENFORCED` correctness finding, which
  blocks the verdict.
- Filter operations run only for FGAC variants. Under plain Glue or full table
  access every row is legitimately visible, so asserting otherwise would
  manufacture a finding rather than detect one.

### Known limitations
- **The data filter path is implemented but not yet validated against a live AWS
  account.** Everything else in this release was validated on EMR Serverless
  across two accounts. Offline coverage exercises the comparison and reporting
  path only.
- Nested (struct) filters are not implemented; the test bed has no nested column.
- EMR on EC2 and EMR on EKS providers are not implemented.
- Each run is independent; there is no cross-run trend view.

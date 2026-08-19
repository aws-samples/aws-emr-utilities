# EMR Test Drive

Run the same Spark workload on two Amazon EMR configurations and get a report that
says what broke, what got slower, and what it costs.

An EMR release upgrade changes several things at once — the Spark version, the
bundled Iceberg/Delta/Hudi versions, the Java runtime, EMR's own patches, and the
default configuration. Any of them can change behaviour. Testing an upgrade by
hand is slow enough that it often does not happen, and the regression is found in
production instead.

EMR Test Drive makes that comparison cheap and repeatable. You describe two or
more **variants** in a YAML file, it runs an identical workload on each, and it
produces a single self-contained HTML report.

```
                  ┌── variant A: emr-7.11.0, plain Glue ──┐
same workload ────┤                                        ├──► one HTML report
                  └── variant B: emr-7.13.0, plain Glue ──┘
```

## What it compares

A variant is any combination of these, so the same tool answers several questions:

| Vary this | Question it answers |
|---|---|
| Release label | Will the upgrade break my jobs? |
| Access mode — plain / Lake Formation FTA / Lake Formation FGAC | What does turning governance on cost me? |
| Spark configuration | Is this tuning change actually an improvement? |
| Architecture (x86_64 / arm64) | What do I gain by moving to Graviton? |
| Custom image | Does my private patch fix the thing it claims to? |

## What the report tells you

Four independent comparisons, in the order that matters:

1. **Correctness** — row counts, ordered result-set checksums, table commit-log
   advancement, and post-operation object listings. Checked first, because a
   faster wrong answer is not an improvement.
2. **Functional** — every operation × table format, diffed against the *documented*
   support matrix. An operation AWS documents as unsupported is reported
   `EXPECTED_UNSUPPORTED`, not as a regression.
3. **Performance** — best-of-N per query, with a noise band derived from the
   observed run-to-run spread. See [docs/methodology.md](docs/methodology.md).
4. **Cost** — billed vCPU-hours and GB-hours per variant.

Each comparison produces a verdict: `PROCEED`, `CAUTION`, `BLOCK`, or
`INDETERMINATE`. The report is one HTML file with no external assets — open it
locally, attach it to a ticket, or serve it as a static page.

## Try it with no AWS account

The offline example runs the comparison engine and report renderer over synthetic
fixtures. Nothing is submitted, nothing is billed.

```bash
git clone https://github.com/aws-samples/aws-emr-utilities
cd aws-emr-utilities/utilities/emr-test-drive
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
make example          # writes examples/offline/out/report.html
```

The example deliberately contains failures, silent data loss, and a patch
validation, so you can see how each finding is presented.

## Run it against your own account

You need one AWS account you are willing to create EMR Serverless applications
in, and about $0.20 per run at the default 40M-row scale.

```bash
cp config.template.yaml my-upgrade.yaml
$EDITOR my-upgrade.yaml            # four required values, everything else defaulted

./etd-cli.py --config my-upgrade.yaml bootstrap   # creates bucket + execution role
./etd-cli.py --config my-upgrade.yaml validate    # checks config and permissions
./etd-cli.py --config my-upgrade.yaml setup       # creates applications, builds test bed
./etd-cli.py --config my-upgrade.yaml run --open
./etd-cli.py --config my-upgrade.yaml teardown --delete-data --delete-iam
```

`bootstrap` and `teardown` are both optional. Teardown is tag-scoped: it only
deletes resources it created (`etd:managed=true` plus the run name), so pointing
it at an account with existing EMR applications will not touch them.

For CI, `run --fail-on new_failure,correctness,regression,timeout` sets a non-zero
exit code so an upgrade that breaks something fails the build.

## Documentation

| | |
|---|---|
| [docs/getting-started.md](docs/getting-started.md) | Full walkthrough, IAM permissions, cost |
| [docs/configuration.md](docs/configuration.md) | Every config key |
| [docs/methodology.md](docs/methodology.md) | How performance is measured and why |
| [docs/interpreting-the-report.md](docs/interpreting-the-report.md) | What each verdict means |
| [docs/lake-formation.md](docs/lake-formation.md) | FTA and FGAC coverage and constraints |
| [docs/design.md](docs/design.md) | Architecture and extension points |

## Status

Validated end to end on **EMR Serverless** across two AWS accounts. EMR on EC2 and
EMR on EKS are designed for but not yet implemented — each is one `Provider`
subclass, see [docs/design.md](docs/design.md).

Lake Formation FGAC creates row, column and cell data filters and **asserts they
were enforced**: a granted filter that returns more than it permits is reported
as a critical correctness finding, because the job succeeds and the data is wrong
in the direction of disclosure. This code path has not yet been validated against
a live account — see [CHANGELOG.md](CHANGELOG.md). Nested (struct) filters are
not implemented: the test bed has no nested column.

## Contributing

See the repository-level [CONTRIBUTING.md](../../CONTRIBUTING.md). Bug reports
that include the generated `report.json` are the most useful kind.

## License

MIT-0, per the [repository LICENSE](../../LICENSE).

## Disclaimer

This utility is not supported by AWS EMR. Use of this code is your
responsibility and at your own risk. It creates billable AWS resources; see
[docs/getting-started.md](docs/getting-started.md) for expected cost and run
`teardown` when finished.
